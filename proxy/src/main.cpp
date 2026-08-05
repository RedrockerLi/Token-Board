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
#include "proxy_server.h"
#include "router.h"
#include "semaphore_pool.h"
#include "upstream_client.h"
#include "usage_tracker.h"

#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <filesystem>

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

    printf("Token Board Proxy\n");
    printf("  DB:   %s\n", cfg.db_path.c_str());
    printf("  Bind: %s:%d\n", cfg.host.c_str(), cfg.port);
    printf("  Log:  %s\n\n", cfg.log_level.c_str());

    // ── Open database ─────────────────────────────────────────────────
    if (cfg.schema_dir.empty()) {
        // Derive the default migration dir from the DB path:
        // data/proxy.db → data/../schema/proxy → schema/proxy
        auto dbp = std::filesystem::path(cfg.db_path);
        cfg.schema_dir = (dbp.parent_path() / ".." / "schema" / "proxy")
                             .lexically_normal()
                             .string();
    }

    Database db;
    if (!db.open(cfg.db_path, cfg.schema_dir)) {
        fprintf(stderr, "FATAL: Cannot open database\n");
        return 1;
    }

    // ── Create components ─────────────────────────────────────────────
    Router router(db);
    UpstreamClient upstream;
    UsageTracker tracker(db);
    CodecRegistry codecs;
    codecs.add(make_openai_codec());
    codecs.add(make_anthropic_codec());
    codecs.add(make_responses_codec());
    ProxyServer proxy_server(db, router, upstream, tracker, codecs);

    // ── Configure httplib server ──────────────────────────────────────
    httplib::Server server;

    // Start with 8 threads and grow only for a real backlog. A bounded ceiling
    // prevents a stalled provider from turning queued requests into thousands
    // of native thread stacks.
    auto *pool = new SemaphorePool(8, 256);
    server.new_task_queue = [pool] { return pool; };

    proxy_server.setup_routes(server);

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
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        auto now = std::chrono::steady_clock::now();

        // ── Auto-scale: double pool when saturated ───────────────────
        int in_flight = proxy_server.in_flight_count();
        size_t cur_size = pool->size();
        // Long-lived streams legitimately occupy every current worker. Grow
        // only when accepted requests are actually queued behind those workers;
        // using in_flight alone doubled the pool every 200 ms during an outage
        // even when there was no backlog.
        if (pool->queued() > 0 && pool->active() >= cur_size &&
            cur_size < pool->max_size()) {
            size_t new_size = std::min(cur_size * 2, pool->max_size());
            fprintf(stderr,
                    "[Scale] %zu → %zu threads (in_flight=%d, queued=%zu)\n",
                    cur_size, new_size, in_flight, pool->queued());
            pool->resize(new_size);
        }

        // ── Periodic cleanup: every 5 min ────────────────────────────
        if (now >= cleanup_deadline) {
            db.cleanup_stale_in_flight(10);     // remove stuck records older than 10 min
            // Session affinity is process-local and no longer writes this
            // legacy observability table on the request hot path.
            cleanup_deadline = now + std::chrono::minutes(5);
        }
    }

    printf("\nShutting down...\n");
    server.stop();
    server_thread.join();

    // Drain and join the accounting thread before closing the DB it writes to.
    proxy_server.shutdown();
    db.close();
    printf("Goodbye.\n");
    return 0;
}
