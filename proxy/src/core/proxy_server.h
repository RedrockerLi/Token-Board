#pragma once

#include "account_gate.h"
#include "candidate_selection.h"
#include "codec.h"
#include "db.h"
#include "endpoint_policy.h"
#include "key_cost_ledger.h"
#include "router.h"
#include "session_affinity.h"
#include "usage_tracker.h"
#include "responses_state_store.h"

#include <atomic>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <list>
#include <memory>
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

using CandidateRequestBody = std::shared_ptr<const std::string>;

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
    struct QueueMetrics {
        std::size_t depth = 0;
        std::size_t active = 0;
        std::size_t workers = 0;
        std::size_t rejected = 0;
        double average_ms = 0.0;
        double p95_ms = 0.0;
        std::int64_t oldest_age_ms = 0;
    };

    ProxyServer(Database &db, Router &router, UpstreamClient &upstream,
                UsageTracker &tracker, CodecRegistry &codecs)
        : db_(db), router_(router), upstream_(upstream), tracker_(tracker),
          codecs_(codecs), affinity_(&cost_ledger_) {
        db_.set_cost_observer([this](int key_slot_id, double cost) {
            cost_ledger_.add(key_slot_id, cost);
        });
        // Cooldown probe cadence: 1h default; TB_COOLDOWN_PROBE_SECS override
        // (tests shrink it to seconds).  Clamped to [1, 86400] seconds.
        const char *env = std::getenv("TB_COOLDOWN_PROBE_SECS");
        const int secs = env ? std::atoi(env) : 3600;
        cooldown_probe_interval_secs_ =
            std::min(86400, std::max(1, secs));
    }

    /// Stops and joins the accounting + cooldown-probe threads.  Idempotent;
    /// also invoked from the destructor.  Must be called BEFORE db_.close() in
    /// the owning main.
    void shutdown() {
        cooldown_probe_stop_.store(true);
        if (cooldown_probe_thread_.joinable()) cooldown_probe_thread_.join();
        db_.set_cost_observer({});
    }

    ~ProxyServer() { shutdown(); }

    /// Start the cooldown-probe background thread.  Idempotent (call from
    /// setup_routes).  Every `cooldown_probe_interval_secs_` it probes plan
    /// keys inside the 5h cooldown and clears the cooldown early when the
    /// upstream reports healthy (2xx GET /models).  The probe never writes
    /// request_log, never counts toward max_concurrency, and touches no gate
    /// counters — it only observes + clears cooldown.
    void start_cooldown_probe();
    void cooldown_probe_loop();
    void run_cooldown_probe_cycle();

    void setup_routes(httplib::Server &server);
    void set_queue_metrics_provider(std::function<QueueMetrics()> provider) {
        queue_metrics_provider_ = std::move(provider);
    }
    int in_flight_count() const noexcept {
        return in_flight_count_.load(std::memory_order_relaxed);
    }
    std::size_t accounting_queue_depth() {
        return db_.log_queue_depth();
    }
    ResponsesStateStore &responses_state() noexcept { return responses_state_; }
    bool try_reserve_accounting() {
        if (auto reservation = db_.reserve_usage_event()) {
            accounting_reservation_ = std::move(reservation);
            return true;
        }
        accounting_rejected_.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    void release_unconsumed_accounting() noexcept {
        accounting_reservation_.reset();
    }
    std::shared_ptr<UsageReservation> detach_accounting_reservation() noexcept {
        auto reservation = std::move(accounting_reservation_);
        accounting_reservation_.reset();
        return reservation;
    }
    void adopt_accounting_reservation(
        const std::shared_ptr<UsageReservation> &reservation) noexcept {
        accounting_reservation_ = reservation;
    }
    /// Record the account/key/model context on the current reservation and mark
    /// it as having begun upstream work, so an abnormal exit (no completed
    /// UsageEvent) writes an internal_abort record instead of silently
    /// releasing the slot.  No-op when no reservation is held.
    void mark_accounting_upstream_started(int account_id, int local_key_id,
                                          const std::string &model,
                                          bool streaming);
    std::uint64_t accounting_dropped() const noexcept {
        return accounting_dropped_.load(std::memory_order_acquire);
    }
    std::uint64_t accounting_rejected() const noexcept {
        return accounting_rejected_.load(std::memory_order_acquire);
    }
    void record_http_result(int status_code) noexcept {
        completed_requests_.fetch_add(1, std::memory_order_relaxed);
        if (status_code >= 400)
            error_requests_.fetch_add(1, std::memory_order_relaxed);
    }
    std::uint64_t completed_requests() const noexcept {
        return completed_requests_.load(std::memory_order_relaxed);
    }
    std::uint64_t error_requests() const noexcept {
        return error_requests_.load(std::memory_order_relaxed);
    }
    QueueMetrics queue_metrics() const {
        return queue_metrics_provider_ ? queue_metrics_provider_() : QueueMetrics{};
    }

private:
    /// Enqueue a request-log record; the response thread pays only an
    /// in-memory move.  Same signature/defaults as UsageTracker::log_request.
    void enqueue_log(int account_id, int local_key_id,
                     const UsageTracker::UsageInfo &usage, bool is_streaming,
                     int status_code, int duration_ms, int upstream_key_id,
                     int ttft_ms, int generation_ms, double output_tps,
                     int upstream_ttft_ms, int upstream_duration_ms,
                     int attempt_count,
                     const std::vector<Database::AttemptInfo> &attempts);

    /// Record a failed/aborted request with zero token usage (status 499 on a
    /// client disconnect, otherwise the truthful upstream status).  Shared by
    /// every non-streaming/streaming tail so failure accounting stays uniform.
    void enqueue_zero_usage(int account_id, int local_key_id,
                            const std::string &model, bool is_streaming,
                            int status_code, int duration_ms,
                            int upstream_key_id, int attempt_count,
                            const std::vector<Database::AttemptInfo> &attempts,
                            int upstream_duration_ms = -1);

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
                          const std::vector<CandidateRequestBody> &candidate_bodies,
                          size_t start, const std::string &session_id,
                          int local_key_id, ir::ApiFormat harness,
                          const std::string &resolved_model,
                          std::shared_ptr<const json> parsed_json,
                          std::shared_ptr<const ir::ChatRequest> parsed_request,
                          std::shared_ptr<const ir::ConversionContext> conversion_context,
                          std::shared_ptr<const std::vector<json>> state_current_input,
                          std::shared_ptr<UsageReservation> reservation,
                          const httplib::Request &req,
                          httplib::Response &res,
                          std::chrono::steady_clock::time_point t0);
    void handle_list_models(const httplib::Request &req,
                            httplib::Response &res);
    void handle_embeddings(const httplib::Request &req,
                           httplib::Response &res);
    std::vector<UpstreamCandidate> resolve_candidates_cached(
        const Router::RouteResult &route, std::string &model);
    Database::TimeoutConfig timeout_config_cached(EndpointKind kind);
    std::uint64_t request_started(const std::string &model, bool streaming);
    void request_finished(std::uint64_t request_id);
    void add_cors_headers(httplib::Response &res);

    Database &db_;
    Router &router_;
    UpstreamClient &upstream_;
    UsageTracker &tracker_;
    CodecRegistry &codecs_;
    AccountGate gate_;           // per-key-slot concurrency + plan cooldown
    KeyCostLedger cost_ledger_;  // accrued cost per key slot (cold-start bias)
    SessionAffinity affinity_;   // session → preferred key (in-memory)
    ResponsesStateStore responses_state_; // complete bounded Responses chains

    struct LiveRequest {
        std::string model;
        bool streaming = false;
        std::chrono::steady_clock::time_point started_at;
    };
    std::atomic<std::uint64_t> next_request_id_{1};
    std::atomic<int> in_flight_count_{0};
    std::atomic<std::uint64_t> completed_requests_{0};
    std::atomic<std::uint64_t> error_requests_{0};
    std::function<QueueMetrics()> queue_metrics_provider_;
    static thread_local std::shared_ptr<UsageReservation> accounting_reservation_;
    // Per-request round-robin offset for in-group key rotation (tier-5
    // routing): each non-affinity group rotates its keys by this counter.
    std::atomic<std::uint64_t> routing_rr_{0};
    mutable std::mutex live_requests_mutex_;
    std::unordered_map<std::uint64_t, LiveRequest> live_requests_;

    // Rejections are sticky health failures; accepted events live in the
    // Database UsageEventWriter's single in-memory queue.
    std::atomic<std::uint64_t> accounting_dropped_{0};
    std::atomic<std::uint64_t> accounting_rejected_{0};

    // Cooldown-probe thread: while a plan key is inside its 5h cooldown, probe
    // the upstream every cooldown_probe_interval_secs_ and clear early on 2xx.
    std::thread cooldown_probe_thread_;
    std::atomic<bool> cooldown_probe_stop_{false};
    std::once_flag cooldown_probe_once_;
    int cooldown_probe_interval_secs_ = 3600;
};
