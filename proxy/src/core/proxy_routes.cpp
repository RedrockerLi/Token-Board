#include "proxy_server_internal.h"

void ProxyServer::setup_routes(httplib::Server &server) {
    // The cooldown-probe thread is started once, alongside route setup (db_ is
    // already open when main constructs ProxyServer).
    start_cooldown_probe();
    // CORS preflight
    auto cors_handler = [this](const httplib::Request &, httplib::Response &res) {
        add_cors_headers(res);
        res.status = 204;
    };
    server.Options("/v1/chat/completions", cors_handler);
    server.Options("/v1/embeddings", cors_handler);
    server.Options("/v1/models", cors_handler);
    server.Options("/v1/messages", cors_handler);
    server.Options("/v1/responses", cors_handler);
    // Double-/v1 aliases: a client whose ANTHROPIC_BASE_URL already ends in
    // "/v1" appends the endpoint path again (e.g. "/v1/v1/messages"). Serve
    // them so such clients work without reconfiguring the base URL.
    server.Options("/v1/v1/chat/completions", cors_handler);
    server.Options("/v1/v1/embeddings", cors_handler);
    server.Options("/v1/v1/models", cors_handler);
    server.Options("/v1/v1/messages", cors_handler);
    server.Options("/v1/v1/responses", cors_handler);

    // The three chat endpoints share one format-agnostic pipeline.
    auto chat_handler = [this](const httplib::Request &req, httplib::Response &res) {
        handle_chat_request(req, res);
    };
    server.Post("/v1/chat/completions", chat_handler);
    server.Post("/v1/messages", chat_handler);
    server.Post("/v1/responses", chat_handler);
    server.Post("/v1/v1/chat/completions", chat_handler);
    server.Post("/v1/v1/messages", chat_handler);
    server.Post("/v1/v1/responses", chat_handler);

    // Embedding endpoint
    server.Post("/v1/embeddings",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_embeddings(req, res);
                });
    server.Post("/v1/v1/embeddings",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_embeddings(req, res);
                });

    // Model listing
    server.Get("/v1/models",
               [this](const httplib::Request &req, httplib::Response &res) {
                   handle_list_models(req, res);
               });
    server.Get("/v1/v1/models",
               [this](const httplib::Request &req, httplib::Response &res) {
                   handle_list_models(req, res);
               });

    // Health check
    server.Get("/health", [this](const httplib::Request &req, httplib::Response &res) {
        add_cors_headers(res);
        json j;
        const auto dropped = accounting_dropped();
        const auto rejected = accounting_rejected();
        const auto persist_failures = db_.log_persist_failures();
        const auto lost_events = db_.log_lost_events();
        const bool writer_healthy = db_.log_writer_healthy();
        const bool recovery_complete = db_.log_recovery_complete();
        const bool routing_ready = router_.snapshot_loaded();
        const auto queue = queue_metrics();
        j["status"] = writer_healthy && recovery_complete && routing_ready &&
                       dropped == 0 && rejected == 0 &&
                       persist_failures == 0 && queue.rejected == 0
                       && lost_events == 0
            ? "ok" : "degraded";
        j["service"] = "token-board-proxy";
        j["schema"] = {{"major", db_.schema_major()},
                        {"minor", db_.schema_minor()}, {"current", true}};
        j["routing"] = {{"loaded", routing_ready},
                         {"generation", router_.snapshot_generation()}};
        j["recovery"] = {{"complete", recovery_complete}};
        j["accounting"] = {{"queue_depth", accounting_queue_depth()},
                           {"oldest_age_ms", db_.log_oldest_age_ms()},
                           {"last_batch_size", db_.log_last_batch_size()},
                           {"last_accounting_ms", db_.log_last_accounting_ms()},
                           {"spool_bytes", db_.log_spool_bytes()},
                           {"writer_healthy", writer_healthy},
                           {"persist_failures", persist_failures},
                           {"lost_events", lost_events},
                           {"dropped", dropped},
                           {"rejected", rejected}};
        j["queue"] = {{"depth", queue.depth},
                       {"active", queue.active},
                       {"workers", queue.workers},
                       {"rejected", queue.rejected},
                       {"average_ms", queue.average_ms},
                       {"p95_ms", queue.p95_ms},
                       {"oldest_age_ms", queue.oldest_age_ms}};
        const auto transport = UpstreamClient::transport_metrics();
        j["transport"] = {
            {"pool_hits", transport.pool_hits},
            {"pool_misses", transport.pool_misses},
            {"clients_created", transport.clients_created},
            {"dns_lookups", transport.dns_lookups},
            {"dns_average_ms", transport.dns_lookups
                ? static_cast<double>(transport.dns_total_ms) /
                      transport.dns_lookups : 0.0},
            {"connect_average_ms", transport.new_connections
                ? static_cast<double>(transport.connect_total_ms) /
                      transport.new_connections : 0.0},
            {"tls_average_ms", transport.new_connections
                ? static_cast<double>(transport.tls_total_ms) /
                      transport.new_connections : 0.0},
            {"new_connections", transport.new_connections},
            {"reused_connections", transport.reused_connections},
            {"lease_count", transport.lease_count},
            {"lease_wait_ms", transport.lease_wait_ms},
            {"active_leases", transport.active_leases}};
        if (!writer_healthy || !recovery_complete || !routing_ready ||
            dropped != 0 || rejected != 0 ||
            persist_failures != 0 || lost_events != 0 || queue.rejected != 0)
            res.status = 503;
        // Live concurrency is a useful local signal (status.sh, dashboard
        // realtime view) but a mild info leak if /health is reachable
        // off-loopback — only report it to loopback clients.
        const std::string &ip = req.remote_addr;
        if (ip == "127.0.0.1" || ip == "::1" ||
            ip.rfind("127.", 0) == 0 || ip.rfind("::ffff:127.", 0) == 0)
            j["concurrency"] = in_flight_count();
        res.set_content(j.dump(), "application/json");
    });
}
