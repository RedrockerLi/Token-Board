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
#include "logging.h"
#include "proxy_server.h"
#include "router.h"
#include "semaphore_pool.h"
#include "upstream_client.h"
#include "usage_recorder.h"

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <thread>

// Signal-safe flag for graceful shutdown
static volatile sig_atomic_t g_shutdown = 0;

extern "C" void signal_handler(int /*signum*/) {
    g_shutdown = 1;
}

int main(int argc, char *argv[]) {
    // Unbuffered stderr for real-time logging
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
    server.task_queue_rejection_handler = [](socket_t sock) {
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

    proxy_server.setup_routes(server);
    server.set_logger([&proxy_server](const httplib::Request &req,
                                      const httplib::Response &res) {
        proxy_server.record_http_result(res.status);
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

    // Wait for signal, with periodic cleanup + auto-scale
    auto cleanup_deadline = std::chrono::steady_clock::now() + std::chrono::minutes(5);
    auto info_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    std::uint64_t last_requests = 0;
    std::uint64_t last_errors = 0;
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        auto now = std::chrono::steady_clock::now();

        if (now >= info_deadline) {
            info_deadline = now + std::chrono::seconds(10);
            const auto requests = proxy_server.completed_requests();
            const auto errors = proxy_server.error_requests();
            const auto interval_requests = requests - last_requests;
            const auto interval_errors = errors - last_errors;
            last_requests = requests;
            last_errors = errors;
            TB_LOG_INFO("[Perf] rps=%.1f errors=%llu workers=%zu active=%zu "
                        "queue=%zu queue_avg=%.2fms queue_p95=%.2fms "
                        "rejected=%zu in_flight=%d\n",
                        interval_requests / 10.0,
                        static_cast<unsigned long long>(interval_errors),
                        pool->size(), pool->active(), pool->queued(),
                        pool->queue_average_ms(), pool->queue_p95_ms(),
                        pool->rejected(), proxy_server.in_flight_count());
        }

        // ── Periodic cleanup: every 5 min ────────────────────────────
        if (now >= cleanup_deadline) {
            cleanup_deadline = now + std::chrono::minutes(5);
        }
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
