#pragma once

#include "account_gate.h"
#include "codec.h"
#include "db.h"
#include "router.h"
#include "usage_tracker.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <list>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
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

/// Bounded, process-local session → upstream-key affinity.
///
/// The hot path is entirely in memory.  A successful fallback is rebound to
/// the key that actually served it, and a bounded O(1) LRU keeps memory use
/// predictable.  On a cold start, rendezvous hashing picks a stable key slot
/// without the wholesale remapping caused by hash(session) % candidate_count.
class SessionAffinity {
public:
    /// Preferred candidate index. `scope` must isolate local credentials and
    /// wire formats; `key_slot_ids` are persistent DB identities, never vector
    /// offsets. Empty sessions deliberately use normal fill-first routing.
    size_t preferred_index(const std::string &scope,
                           const std::string &session_id,
                           const std::vector<int> &key_slot_ids) {
        if (session_id.empty() || key_slot_ids.empty()) return 0;
        const std::string key = digest(scope, session_id);
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(key);
        if (it != bindings_.end()) {
            if (now - it->second.last_seen > ttl_) {
                lru_.erase(it->second.lru_it);
                bindings_.erase(it);
            } else {
                it->second.last_seen = now;
                lru_.splice(lru_.begin(), lru_, it->second.lru_it);
                for (size_t i = 0; i < key_slot_ids.size(); ++i) {
                    if (key_slot_ids[i] == it->second.key_slot_id) return i;
                }
            }
        }

        // Rendezvous hashing: choose the candidate with the greatest score.
        size_t best = 0;
        uint64_t best_score = 0;
        for (size_t i = 0; i < key_slot_ids.size(); ++i) {
            uint64_t score = hash64(key + "#" + std::to_string(key_slot_ids[i]));
            if (i == 0 || score > best_score) {
                best = i;
                best_score = score;
            }
        }
        return best;
    }

    /// Bind only after a successful upstream response.  Rebinding is local,
    /// so affinity never adds a synchronous SQLite write to a request.
    void bind(const std::string &scope, const std::string &session_id,
              int key_slot_id) {
        if (session_id.empty()) return;
        const std::string key = digest(scope, session_id);
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(key);
        if (it != bindings_.end()) {
            it->second.key_slot_id = key_slot_id;
            it->second.last_seen = now;
            lru_.splice(lru_.begin(), lru_, it->second.lru_it);
            return;
        }
        while (bindings_.size() >= max_ && !lru_.empty()) {
            bindings_.erase(lru_.back());
            lru_.pop_back();
        }
        lru_.push_front(key);
        bindings_.emplace(key, Entry{key_slot_id, now, lru_.begin()});
    }

private:
    static uint64_t hash64(const std::string &value) {
        // FNV-1a is deterministic and keeps the raw session id out of the
        // map. This is an in-memory routing key, not a cryptographic digest.
        uint64_t h = 1469598103934665603ULL;
        for (unsigned char c : value) { h ^= c; h *= 1099511628211ULL; }
        return h;
    }
    static std::string digest(const std::string &scope,
                              const std::string &session_id) {
        return std::to_string(hash64(scope + "\x1f" + session_id));
    }
    struct Entry {
        int key_slot_id;
        std::chrono::steady_clock::time_point last_seen;
        std::list<std::string>::iterator lru_it;
    };

    static constexpr size_t max_ = 100000;
    std::chrono::hours ttl_{24};
    std::mutex mutex_;
    std::list<std::string> lru_;
    std::unordered_map<std::string, Entry> bindings_;
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
    // Aggregate entry ordinal.  Keys belonging to one entry may spill over
    // among themselves, but an entry with a larger group is only considered
    // after every candidate in an earlier group was unavailable or failed.
    int priority_group = 0;
};

/// Build the candidate retry order: groups are tried in priority order
/// (cheaper/higher-priority first), and WITHIN each group the keys are rotated
/// so a group's multiple keys wear evenly.  The group containing the
/// affinity-preferred candidate (`preferred_index`, a global index into
/// `candidates`) starts at that key; every other group starts at a per-request
/// round-robin offset.  Cross-group usage is deliberately NOT balanced — a
/// cheaper upstream should be used more, per aggregate sort_order.
inline std::vector<size_t> candidate_order(
    const std::vector<UpstreamCandidate> &candidates,
    size_t preferred_index, size_t rr_offset) {
    std::vector<size_t> order;
    if (candidates.empty()) return order;
    size_t i = 0;
    while (i < candidates.size()) {
        const int group = candidates[i].priority_group;
        const size_t begin = i;
        while (i < candidates.size() &&
               candidates[i].priority_group == group) ++i;
        const size_t size = i - begin;
        const size_t start = (preferred_index >= begin && preferred_index < i)
            ? (preferred_index - begin) % size
            : rr_offset % size;
        for (size_t k = 0; k < size; ++k)
            order.push_back(begin + (start + k) % size);
    }
    return order;
}

/// Failures tied to one configured credential/provider candidate should spill
/// over before the proxy gives up.  401/403 are candidate-specific for a
/// multi-key account just as 429 is; retrying another key is both safe and
/// required when one subscription credential was revoked.
inline bool candidate_failure_retryable(int status_code) {
    return status_code == 401 || status_code == 403 ||
           status_code == 429 || status_code >= 500;
}

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

    /// Stops and joins the accounting thread.  Idempotent; also invoked from
    /// the destructor.  Must be called BEFORE db_.close() in the owning main.
    void shutdown() {
        {
            std::lock_guard<std::mutex> lock(accounting_mutex_);
            accounting_stop_ = true;
        }
        accounting_cv_.notify_all();
        if (accounting_thread_.joinable()) accounting_thread_.join();
    }

    ~ProxyServer() { shutdown(); }

    void setup_routes(httplib::Server &server);
    int in_flight_count() const noexcept {
        return in_flight_count_.load(std::memory_order_relaxed);
    }

private:
    /// One request-log record enqueued by the response thread and drained by
    /// the accounting thread.  Copying bounds are enforced by the DB layer
    /// (kLogAttemptsMax, kLogErrorMaxBytes).
    struct LogJob {
        int account_id = 0;
        int local_key_id = 0;
        UsageTracker::UsageInfo usage;
        bool is_streaming = false;
        int status_code = 0;
        int duration_ms = 0;
        int upstream_key_id = 0;
        int ttft_ms = -1;
        int generation_ms = -1;
        double output_tps = -1.0;
        int upstream_ttft_ms = -1;
        int upstream_duration_ms = -1;
        int attempt_count = 1;
        std::vector<Database::AttemptInfo> attempts;
    };

    /// Enqueue a request-log record; the response thread pays only an
    /// in-memory move.  Same signature/defaults as UsageTracker::log_request.
    void enqueue_log(int account_id, int local_key_id,
                     const UsageTracker::UsageInfo &usage, bool is_streaming,
                     int status_code, int duration_ms, int upstream_key_id,
                     int ttft_ms, int generation_ms, double output_tps,
                     int upstream_ttft_ms, int upstream_duration_ms,
                     int attempt_count,
                     const std::vector<Database::AttemptInfo> &attempts);
    void accounting_loop();

    void handle_chat_request(const httplib::Request &req,
                             httplib::Response &res);
    /// Streaming chat path with in-request fallback.  Tries candidates from
    /// `start` cyclically; each candidate is forwarded as passthrough (when its
    /// format matches the harness) or converted, decided per candidate.  The
    /// upstream status is read (via response_handler) before the first
    /// sink.write, and the server's chunked response headers are DEFERRED until
    /// that write — so a 429/5xx from one key falls through to the next
    /// key/upstream within the same request, and only after every candidate
    /// fails is an error status (429/504) committed to the client.
    void handle_streaming(const std::vector<UpstreamCandidate> &cands,
                          size_t start, const std::string &session_id,
                          int local_key_id, ir::ApiFormat harness,
                          const std::string &resolved_model,
                          const httplib::Request &req,
                          httplib::Response &res,
                          std::chrono::steady_clock::time_point t0);
    void handle_list_models(const httplib::Request &req,
                            httplib::Response &res);
    void handle_embeddings(const httplib::Request &req,
                           httplib::Response &res);
    std::vector<UpstreamCandidate> resolve_candidates_cached(
        const Router::RouteResult &route, std::string &model);
    Database::TimeoutConfig timeout_config_cached(ir::ApiFormat harness);
    std::uint64_t request_started(const std::string &model, bool streaming);
    void request_finished(std::uint64_t request_id);
    void add_cors_headers(httplib::Response &res);

    Database &db_;
    Router &router_;
    UpstreamClient &upstream_;
    UsageTracker &tracker_;
    CodecRegistry &codecs_;
    AccountGate gate_;           // per-key-slot concurrency + plan cooldown
    SessionAffinity affinity_;   // session → preferred key (in-memory)

    struct CandidateCacheEntry {
        std::vector<UpstreamCandidate> candidates;
        std::chrono::steady_clock::time_point expires_at;
    };
    std::mutex candidate_cache_mutex_;
    std::condition_variable candidate_cache_cv_;
    std::unordered_set<std::string> candidate_cache_loading_;
    std::unordered_map<std::string, CandidateCacheEntry> candidate_cache_;

    struct TimeoutCacheEntry {
        Database::TimeoutConfig config;
        std::chrono::steady_clock::time_point expires_at{};
        bool valid = false;
    };
    std::mutex timeout_cache_mutex_;
    std::unordered_map<int, TimeoutCacheEntry> timeout_cache_;

    struct LiveRequest {
        std::string model;
        bool streaming = false;
        std::chrono::steady_clock::time_point started_at;
    };
    std::atomic<std::uint64_t> next_request_id_{1};
    std::atomic<int> in_flight_count_{0};
    // Per-request round-robin offset for in-group key rotation (tier-5
    // routing): each non-affinity group rotates its keys by this counter.
    std::atomic<std::uint64_t> routing_rr_{0};
    mutable std::mutex live_requests_mutex_;
    std::unordered_map<std::uint64_t, LiveRequest> live_requests_;

    // Request-log accounting is drained by a dedicated thread so the response
    // path never pays pricing SELECT + JSON + spool write synchronously.
    static constexpr std::size_t kAccountingQueueMax = 16384;
    std::mutex accounting_mutex_;
    std::condition_variable accounting_cv_;
    std::list<LogJob> accounting_queue_;
    std::thread accounting_thread_;
    bool accounting_stop_ = false;
    std::once_flag accounting_thread_once_;
    std::atomic<std::uint64_t> accounting_dropped_{0};
};
