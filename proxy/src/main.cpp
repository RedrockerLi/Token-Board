/// Token Board Proxy — High-performance OpenAI-compatible API proxy.
///
/// Listens for chat completions requests from AI tools, routes them to
/// different CSTCloud upstream accounts based on the local API key, and
/// tracks per-account token usage + billing in SQLite.

#include "config.h"
#include "db.h"
#include "proxy_server.h"
#include "router.h"
#include "upstream_client.h"
#include "usage_tracker.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <chrono>

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
    Database db;
    if (!db.open(cfg.db_path)) {
        fprintf(stderr, "FATAL: Cannot open database\n");
        return 1;
    }

    // ── Create components ─────────────────────────────────────────────
    Router router(db);
    UpstreamClient upstream;
    UsageTracker tracker(db);
    ProxyServer proxy_server(db, router, upstream, tracker);

    // ── Configure httplib server ──────────────────────────────────────
    httplib::Server server;

    // Multi-threading: use a thread pool for concurrent connections
    // 8 threads should handle typical proxy load (all I/O bound)
    server.new_task_queue = [] { return new httplib::ThreadPool(8); };

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

    // Wait for signal, with periodic perf_events cleanup (every 5 min)
    auto cleanup_deadline = std::chrono::steady_clock::now() + std::chrono::minutes(5);
    while (!g_shutdown) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

        auto now = std::chrono::steady_clock::now();
        if (now >= cleanup_deadline) {
            db.cleanup_old_perf_events(1440);  // keep last 24 hours
            db.cleanup_stale_in_flight(10);     // remove stuck records older than 10 min
            cleanup_deadline = now + std::chrono::minutes(5);
        }
    }

    printf("\nShutting down...\n");
    server.stop();
    server_thread.join();

    db.close();
    printf("Goodbye.\n");
    return 0;
}
