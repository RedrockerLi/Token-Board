#pragma once

class Database;
class Router;
class UpstreamClient;
class UsageTracker;
class ModelPricing;

namespace httplib {
class Server;
struct Request;
struct Response;
} // namespace httplib

/// Configures the httplib::Server with route handlers.
///
/// Registers:
///   POST /v1/chat/completions   — main proxy endpoint
///   GET  /health                  — health-check
///
/// All routes include CORS headers (Access-Control-Allow-Origin: *).
class ProxyServer {
public:
    ProxyServer(Database &db, Router &router, UpstreamClient &upstream,
                UsageTracker &tracker, ModelPricing &pricing)
        : db_(db), router_(router), upstream_(upstream), tracker_(tracker),
          pricing_(pricing) {}

    void setup_routes(httplib::Server &server);

private:
    void handle_chat_completions(const httplib::Request &req,
                                 httplib::Response &res);
    void handle_list_models(const httplib::Request &req,
                            httplib::Response &res);
    void handle_embeddings(const httplib::Request &req,
                           httplib::Response &res);
    void add_cors_headers(httplib::Response &res);

    Database &db_;
    Router &router_;
    UpstreamClient &upstream_;
    UsageTracker &tracker_;
    ModelPricing &pricing_;
};
