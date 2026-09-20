/// Token Board Proxy — High-performance OpenAI-compatible API proxy.
///
/// Listens for chat completions requests from AI tools, routes them to
/// different CSTCloud upstream accounts based on the local API key, and
/// tracks per-account token usage + billing in SQLite.

// httplib.h MUST come first, with CPPHTTPLIB_OPENSSL_SUPPORT set,
// so that the SSL-enabled definitions are visible to all later includes
// (including semaphore_pool.h via its forward references).
#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include "config.h"
#include "db.h"
#include "format_anthropic.h"
#include "format_openai.h"
#include "format_responses.h"
#include "http_debug_log.h"
#include "logging.h"
#include "proxy_server.h"
#include "proxy_error_log.h"
#include "router.h"
#include "semaphore_pool.h"
#include "upstream_client.h"
#include "usage_recorder.h"

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <thread>

// Signal-safe flag for graceful shutdown
static volatile sig_atomic_t g_shutdown = 0;

extern "C" void signal_handler(int /*signum*/) {
    g_shutdown = 1;
}

int main(int argc, char *argv[]) {
    // Unbuffered output for real-time foreground debugging and service logs.
    setbuf(stdout, NULL);
    setbuf(stderr, NULL);

    // ── Parse config ──────────────────────────────────────────────────
    Config cfg = parse_args(argc, argv);
    if (!set_log_level(cfg.log_level)) {
        TB_LOG_ERROR( "Invalid --log-level: %s\n", cfg.log_level.c_str());
        return 2;
    }

    printf("Token Board Proxy\n");
    printf("  DB:   %s\n", cfg.db_path.c_str());
    printf("  Bind: %s:%d\n", cfg.host.c_str(), cfg.port);
    printf("  Log:  %s\n\n", cfg.log_level.c_str());

    // ── Open database ─────────────────────────────────────────────────
    Database db;
    if (!db.open(cfg.db_path)) {
        TB_LOG_ERROR( "FATAL: Cannot open database\n");
        return 1;
    }
    if (db.schema_major() != 2) {
        TB_LOG_ERROR(
                "FATAL: Runtime requires a V2 database; run the "
                "Python schema-upgrade boundary first\n");
        db.close();
        return 1;
    }

    // ── Create components ─────────────────────────────────────────────
    Router router(db);
    UpstreamClient upstream;
    UsageRecorder recorder(db);
    CodecRegistry codecs;
    codecs.add(make_openai_codec());
    codecs.add(make_anthropic_codec());
    codecs.add(make_responses_codec());
    ProxyServer proxy_server(db, router, upstream, recorder, codecs);

    // ── Configure httplib server ──────────────────────────────────────
    httplib::Server server;

    // Start at 2× logical CPUs (bounded to 8..64) and grow on enqueue. A bounded ceiling
    // prevents a stalled provider from turning queued requests into thousands
    // of native thread stacks.
    const auto cpu_count = std::max(1u, std::thread::hardware_concurrency());
    size_t max_workers = 512;
    if (const char *configured = std::getenv("TB_MAX_WORKERS")) {
        const auto parsed = std::strtoull(configured, nullptr, 10);
        if (parsed > 0) max_workers = std::min<std::size_t>(parsed, 512);
    }
    const size_t initial_workers = std::min(
        max_workers, std::clamp<size_t>(cpu_count * 2, 8, 64));
    size_t task_queue_max = 4096;
    if (const char *configured = std::getenv("TB_TASK_QUEUE_MAX")) {
        const auto parsed = std::strtoull(configured, nullptr, 10);
        if (parsed > 0) task_queue_max = std::min<std::size_t>(parsed, 1'000'000);
    }
    auto *pool = new SemaphorePool(initial_workers, max_workers, task_queue_max);
    server.new_task_queue = [pool] { return pool; };
    proxy_server.set_queue_metrics_provider([pool] {
        return ProxyServer::QueueMetrics{
            pool->queued(), pool->active(), pool->size(), pool->rejected(),
            pool->queue_average_ms(), pool->queue_p95_ms(),
            pool->queue_oldest_age_ms()};
    });
    server.task_queue_rejection_handler = [&proxy_server](socket_t sock) {
        ProxyLogContext log_context;
        log_context.request_id = proxy_server.allocate_request_id();
        log_context.method = "UNKNOWN";
        log_context.endpoint = "unknown";
        log_proxy_failure(log_context, "framework", "proxy_queue_full", 503);
        static constexpr char response[] =
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 54\r\n"
            "Retry-After: 1\r\n"
            "Connection: close\r\n\r\n"
            "{\"error\":{\"message\":\"Proxy queue is full\",\"code\":503}}";
        const char *cursor = response;
        std::size_t remaining = sizeof(response) - 1;
        while (remaining != 0) {
            const auto written = httplib::detail::send_socket(
                sock, cursor, remaining, CPPHTTPLIB_SEND_FLAGS);
            if (written <= 0) break;
            cursor += written;
            remaining -= static_cast<std::size_t>(written);
        }
    };

    server.set_pre_routing_handler(
        [](const httplib::Request &, httplib::Response &) {
            tb_http_debug::reset_downstream_request_state();
            reset_proxy_error_log_scope();
            return httplib::Server::HandlerResponse::Unhandled;
        });
    proxy_server.setup_routes(server);
    server.set_error_handler([&proxy_server](const httplib::Request &req,
                                              httplib::Response &res) {
        // Business handlers log their own 4xx/5xx responses. These statuses
        // are emitted by cpp-httplib before a handler runs (or for an
        // unsupported method/path), so they need a framework-level record.
        if (res.status == 404 || res.status == 405 || res.status == 413 ||
            res.status == 414 || res.status == 431) {
            ProxyLogContext log_context;
            log_context.request_id = proxy_server.allocate_request_id();
            log_context.method = req.method;
            log_context.endpoint = req.path;
            log_context.format = "http";
            const char *reason = res.status == 404 ? "route_not_found"
                : res.status == 405 ? "method_not_allowed"
                : res.status == 413 ? "request_body_too_large"
                : res.status == 414 ? "request_target_too_large"
                                     : "request_headers_too_large";
            log_proxy_failure(log_context, "framework", reason, res.status);
        }
        return httplib::Server::HandlerResponse::Unhandled;
    });
    server.set_exception_handler(
        [&proxy_server](const httplib::Request &req, httplib::Response &res,
                        std::exception_ptr exception) {
            ProxyLogContext log_context;
            log_context.request_id = proxy_server.allocate_request_id();
            log_context.method = req.method;
            log_context.endpoint = req.path;
            log_context.format = "http";
            log_proxy_failure(log_context, "framework", "handler_exception", 500);
            res.status = 500;
            // Preserve cpp-httplib's existing response contract while keeping
            // the exception out of the new structured journal record.
            if (!exception) {
                res.set_header("EXCEPTION_WHAT", "UNKNOWN");
                return;
            }
            try {
                std::rethrow_exception(exception);
            } catch (const std::exception &error) {
                const char *message = error.what();
                std::string escaped;
                if (message) {
                    for (const char *cursor = message; *cursor; ++cursor) {
                        if (*cursor == '\r') escaped += "\\r";
                        else if (*cursor == '\n') escaped += "\\n";
                        else escaped += *cursor;
                    }
                }
                res.set_header("EXCEPTION_WHAT", escaped);
            } catch (...) {
                res.set_header("EXCEPTION_WHAT", "UNKNOWN");
            }
        });
    server.set_logger([&proxy_server](const httplib::Request &req,
                                      const httplib::Response &res) {
        tb_http_debug::downstream_request_if_missing(req);
        tb_http_debug::downstream_response(req, res);
        const bool structured_error = consume_proxy_error_log_scope();
        if (res.status >= 400 && res.status <= 599 && !structured_error) {
            ProxyLogContext log_context;
            log_context.request_id = proxy_server.allocate_request_id();
            log_context.method = req.method;
            log_context.endpoint = req.path;
            log_context.format = "http";
            const char *reason = res.status == 400 ? "http_bad_request"
                : res.status == 404 ? "route_not_found"
                : res.status == 405 ? "method_not_allowed"
                : res.status == 413 ? "request_body_too_large"
                : res.status == 414 ? "request_target_too_large"
                : res.status == 416 ? "invalid_range"
                : res.status == 431 ? "request_headers_too_large"
                                     : "http_response_error";
            log_proxy_failure(log_context, "framework", reason, res.status);
        }
        TB_LOG_DEBUG("[HTTP] %s %s status=%d\n", req.method.c_str(),
                     req.path.c_str(), res.status);
    });

    // ── Graceful shutdown ─────────────────────────────────────────────
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    printf("Proxy listening on http://%s:%d\n", cfg.host.c_str(), cfg.port);
    printf("Press Ctrl+C to stop.\n\n");

    // Start server in a separate thread so we can poll for shutdown
    auto server_thread = std::thread([&]() {
        server.listen(cfg.host.c_str(), cfg.port);
    });

    // Wait for signal while the server runs.
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    printf("\nShutting down...\n");
    server.stop();
    server_thread.join();

    // Drain and join the accounting thread before closing the DB it writes to.
    proxy_server.shutdown();
    router.shutdown();
    db.close();
    printf("Goodbye.\n");
    return 0;
}
