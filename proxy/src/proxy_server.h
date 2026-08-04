#pragma once

#include "account_gate.h"
#include "codec.h"
#include "db.h"
#include "router.h"

#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
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

/// Session → preferred-key affinity (cache-friendly).
///
/// Preferred key = hash(session_id) % n_candidates: a pure function, so the
/// same session always prefers the same key with no mutable routing state.
/// A bounded in-memory LRU only dedups the session_key_log writes (a binding
/// is logged once, when first seen or when it changes).
class SessionAffinity {
public:
    /// Preferred candidate index for a session; 0 for empty session (fall
    /// back to plain fill-first).
    int preferred_index(const std::string &session_id, size_t n_cands) const {
        if (session_id.empty() || n_cands == 0) return 0;
        return static_cast<int>(std::hash<std::string>{}(session_id) % n_cands);
    }
    /// True when the (session → key) binding is new or changed — the caller
    /// writes one session_key_log row. Bounded LRU; stale entries dropped.
    bool binding_changed(const std::string &session_id, int key_id) {
        if (session_id.empty()) return false;
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(session_id);
        if (it != bindings_.end()) {
            if (now - it->second.second > ttl_) {
                bindings_.erase(it);
            } else if (it->second.first == key_id) {
                it->second.second = now;
                return false;
            }
        }
        if (bindings_.size() >= max_) {
            auto oldest = bindings_.begin();
            for (auto jt = bindings_.begin(); jt != bindings_.end(); ++jt)
                if (jt->second.second < oldest->second.second) oldest = jt;
            bindings_.erase(oldest);
        }
        bindings_[session_id] = {key_id, now};
        return true;
    }

private:
    static constexpr size_t max_ = 10000;
    std::chrono::minutes ttl_{30};
    std::mutex mutex_;
    // session → (last logged key_id, last seen)
    std::unordered_map<std::string,
                       std::pair<int, std::chrono::steady_clock::time_point>>
        bindings_;
};

/// One real upstream target a request may be forwarded to.  Plain accounts
/// yield one candidate per configured key; aggregate accounts yield one
/// candidate per (matching entry × its account's keys), in priority order.
/// `key` overrides `account.upstream_key` for the forward (per-key value);
/// `key_slot_id` is the concurrency identity (-1 for an account's legacy
/// single key).
struct UpstreamCandidate {
    Database::AccountInfo account;  // real account (complete type from db.h)
    std::string key;                // the key to forward with
    int key_slot_id = -1;           // concurrency slot id (upstream_keys.id)
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
    AccountGate gate_;           // per-key-slot concurrency + plan cooldown
    SessionAffinity affinity_;   // session → preferred key (in-memory)
};
