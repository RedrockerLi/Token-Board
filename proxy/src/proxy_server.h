#pragma once

#include "account_gate.h"
#include "codec.h"
#include "db.h"
#include "router.h"

#include <vector>

class Database;
class Router;
class UpstreamClient;
class UsageTracker;

namespace httplib {
class Server;
struct Request;
struct Response;
} // namespace httplib

/// One real upstream target a request may be forwarded to.  Plain accounts
/// yield exactly one candidate; aggregate accounts yield one candidate per
/// matching model entry, in priority order (sort_order, id).
struct UpstreamCandidate {
    Database::AccountInfo account;  // real account (complete type from db.h)
    std::string upstream_model;     // model name forwarded upstream
};

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
    /// Streaming-only passthrough for one already-picked candidate.
    void handle_passthrough(const UpstreamCandidate &cand, int local_key_id,
                            ir::ApiFormat upstream,
                            const std::string &resolved_model,
                            const httplib::Request &req,
                            httplib::Response &res,
                            std::chrono::steady_clock::time_point t0);
    /// Streaming-only converted path for one already-picked candidate.
    void handle_converted(const UpstreamCandidate &cand, int local_key_id,
                          ir::ApiFormat harness, ir::ApiFormat upstream,
                          const std::string &resolved_model,
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
    AccountGate gate_;   // per-account concurrency + plan cooldown (in-memory)
};
