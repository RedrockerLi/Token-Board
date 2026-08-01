#pragma once

#include "codec.h"
#include "router.h"

class Database;
class Router;
class UpstreamClient;
class UsageTracker;

namespace httplib {
class Server;
struct Request;
struct Response;
} // namespace httplib

/// Configures the httplib::Server with route handlers.
///
/// Registers:
///   POST /v1/chat/completions   — OpenAI-compatible proxy endpoint
///   POST /v1/messages           — Anthropic-compatible proxy endpoint
///   POST /v1/responses          — OpenAI Responses proxy endpoint
///   GET  /health                — health-check
///
/// The three chat endpoints share one pipeline: the harness (client) format is
/// derived from the incoming URL path (/v1/chat/completions → OpenAI,
/// /v1/responses → Responses, /v1/messages → Anthropic), and converted to the
/// account's upstream format via the codec registry when they differ.
class ProxyServer {
public:
    ProxyServer(Database &db, Router &router, UpstreamClient &upstream,
                UsageTracker &tracker, CodecRegistry &codecs)
        : db_(db), router_(router), upstream_(upstream), tracker_(tracker),
          codecs_(codecs) {}

    void setup_routes(httplib::Server &server);

private:
    void handle_chat_request(const httplib::Request &req,
                             httplib::Response &res);
    void handle_passthrough(const Router::RouteResult &route,
                            ir::ApiFormat upstream,
                            const httplib::Request &req,
                            httplib::Response &res,
                            std::chrono::steady_clock::time_point t0);
    void handle_converted(const Router::RouteResult &route,
                          ir::ApiFormat harness, ir::ApiFormat upstream,
                          const httplib::Request &req,
                          httplib::Response &res,
                          std::chrono::steady_clock::time_point t0);
    void handle_list_models(const httplib::Request &req,
                            httplib::Response &res);
    void handle_embeddings(const httplib::Request &req,
                           httplib::Response &res);
    void add_cors_headers(httplib::Response &res);

    Database &db_;
    Router &router_;
    UpstreamClient &upstream_;
    UsageTracker &tracker_;
    CodecRegistry &codecs_;
};
