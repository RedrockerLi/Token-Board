#include "proxy_server.h"
#include "account_types.h"
#include "db.h"
#include "format_common.h"
#include "router.h"
#include "think_filter.h"
#include "upstream_client.h"
#include "usage_tracker.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <poll.h>
#include <sys/socket.h>
#include <thread>
#include <utility>

using json = nlohmann::json;

/// Balance every successful AccountGate acquisition even when request-body
/// conversion, the HTTP client, or response parsing throws. A lease is
/// released as soon as the upstream call has returned: logging and response
/// serialization must never consume an upstream concurrency slot.
class GateLease {
public:
    GateLease(AccountGate &gate, int key_slot_id) noexcept
        : gate_(&gate), key_slot_id_(key_slot_id) {}
    GateLease(const GateLease &) = delete;
    GateLease &operator=(const GateLease &) = delete;
    ~GateLease() { release(); }

    void release() {
        if (!gate_) return;
        gate_->release(key_slot_id_);
        gate_ = nullptr;
    }

private:
    AccountGate *gate_;
    int key_slot_id_;
};

template <typename F>
class ScopeExit {
public:
    explicit ScopeExit(F fn) : fn_(std::move(fn)) {}
    ScopeExit(const ScopeExit &) = delete;
    ScopeExit &operator=(const ScopeExit &) = delete;
    ~ScopeExit() { run_now(); }

    void run_now() {
        if (!active_) return;
        active_ = false;
        fn_();
    }

private:
    F fn_;
    bool active_ = true;
};

template <typename F>
ScopeExit<F> make_scope_exit(F fn) {
    return ScopeExit<F>(std::move(fn));
}

// ── Helpers ──────────────────────────────────────────────────────────────

/// Check the parsed JSON shape instead of a substring (a prompt can contain
/// `"stream": true` as ordinary text).
static bool is_streaming_request(const std::string &body) {
    try {
        auto j = json::parse(body);
        return j.is_object() && j.value("stream", false);
    } catch (...) {
        return false;
    }
}

/// Extract model name without confusing escaped prompt text for a field.
static std::string extract_model(const std::string &body) {
    try {
        auto j = json::parse(body);
        if (j.is_object() && j.contains("model") && j["model"].is_string())
            return j["model"].get<std::string>();
    } catch (...) {}
    return "unknown";
}

/// Best-effort session identifier for session-affinity routing.
///
/// Precedence: an explicit `x-session-id` / `x-conversation-id` header, then a
/// stable body field the client actually sends (OpenAI `user`, Anthropic
/// `metadata.user_id`, Responses `previous_response_id`).  Empty result →
/// plain fill-first (no affinity).  A stable value keeps the same session on
/// the same upstream key for cache affinity.
static std::string extract_session_id(const httplib::Request &req,
                                      const json &req_json) {
    std::string hdr = req.get_header_value("x-session-id");
    if (hdr.empty()) hdr = req.get_header_value("x-conversation-id");
    if (!hdr.empty()) return hdr;
    if (req_json.is_object()) {
        if (req_json.contains("user") && req_json["user"].is_string())
            return req_json["user"].get<std::string>();
        if (req_json.contains("metadata") && req_json["metadata"].is_object()) {
            const auto &md = req_json["metadata"];
            if (md.contains("user_id") && md["user_id"].is_string())
                return md["user_id"].get<std::string>();
        }
        if (req_json.contains("previous_response_id") &&
            req_json["previous_response_id"].is_string())
            return req_json["previous_response_id"].get<std::string>();
    }
    return "";
}

static std::string affinity_scope(int local_key_id, ir::ApiFormat harness) {
    return std::to_string(local_key_id) + ":" + ir::to_string(harness);
}

/// Gray-scale switch for truncated-stream classification.  When disabled, a
/// clean EOF before a terminal event is treated as success (pre-fix behavior)
/// for providers that do not emit a standard terminal frame.  Default: strict
/// classification on (providers like OpenAI/Anthropic always emit one).
static bool strict_terminal_enabled() {
    const char *v = std::getenv("PROXY_STRICT_TERMINAL");
    if (v == nullptr) return true;
    return std::string(v) != "0";
}

static size_t affinity_start(SessionAffinity &affinity,
                             const std::string &scope,
                             const std::string &session_id,
                             const std::vector<UpstreamCandidate> &cands) {
    // Affinity spans EVERY candidate's key slot, not just the first priority
    // group: any account (cheap or expensive) with multiple keys deserves the
    // same in-group affinity + wear-leveling.  The returned index is a global
    // index into `cands` (slots below mirror the candidate order), and
    // candidate_order later maps it to a per-group rotation start while still
    // trying cheaper groups first.
    std::vector<int> slots;
    if (cands.empty()) return 0;
    slots.reserve(cands.size());
    for (const auto &cand : cands) slots.push_back(cand.key_slot_id);
    return affinity.preferred_index(scope, session_id, slots);
}

/// Responses API chains carry the previous response ID, so retain the ID
/// emitted by a successful response as an alias for the same key slot.
static std::string response_id_from_body(const std::string &body) {
    auto read_id = [](const json &j) -> std::string {
        if (j.is_object() && j.contains("id") && j["id"].is_string())
            return j["id"].get<std::string>();
        if (j.is_object() && j.contains("response") && j["response"].is_object() &&
            j["response"].contains("id") && j["response"]["id"].is_string())
            return j["response"]["id"].get<std::string>();
        return "";
    };
    try {
        if (auto id = read_id(json::parse(body)); !id.empty()) return id;
    } catch (...) {}
    // Streaming SSE: inspect each complete `data:` JSON payload.  The first
    // response.created event normally carries the stable Responses id.
    size_t pos = 0;
    while ((pos = body.find("data:", pos)) != std::string::npos) {
        size_t end = body.find('\n', pos);
        std::string payload = body.substr(pos + 5,
            end == std::string::npos ? std::string::npos : end - pos - 5);
        while (!payload.empty() && (payload.front() == ' ' || payload.front() == '\r'))
            payload.erase(payload.begin());
        try { if (auto id = read_id(json::parse(payload)); !id.empty()) return id; }
        catch (...) {}
        pos = end == std::string::npos ? body.size() : end + 1;
    }
    return "";
}

/// Best-effort semantic activity detector used only when a provider emits a
/// valid extension the format-neutral parser does not yet model (for example
/// OpenAI audio deltas). It never changes or delays payload bytes. Known
/// metadata, usage, terminal and heartbeat frames are deliberately ignored so
/// they cannot defeat the pre-semantic timeout.
class SemanticFallbackObserver {
public:
    bool feed(const char *data, size_t len) {
        bool semantic = false;
        frames_.feed(data, len, [&](const std::string &frame) {
            semantic = frame_is_semantic(frame) || semantic;
        });
        return semantic;
    }

    bool finish() {
        bool semantic = false;
        frames_.finish([&](const std::string &frame) {
            semantic = frame_is_semantic(frame) || semantic;
        });
        return semantic;
    }

private:
    static bool has_suffix(const std::string &value,
                           const std::string &suffix) {
        return value.size() >= suffix.size() &&
               value.compare(value.size() - suffix.size(), suffix.size(),
                             suffix) == 0;
    }

    static bool nonempty_value(const json &value) {
        if (value.is_null()) return false;
        if (value.is_string()) return !value.get_ref<const std::string &>().empty();
        if (value.is_array() || value.is_object()) return !value.empty();
        return true;
    }

    static bool frame_is_semantic(const std::string &frame) {
        std::string event_name;
        std::string payload;
        if (!fmt::parse_sse_frame(frame, &event_name, &payload) ||
            payload.empty() || payload == "[DONE]")
            return false;

        json value;
        try {
            value = json::parse(payload);
        } catch (...) {
            return false;
        }
        if (!value.is_object() || value.contains("error")) return false;

        // OpenAI Chat Completions: any non-empty delta member other than the
        // role is user-visible/model output, including provider extensions.
        if (value.contains("choices") && value["choices"].is_array()) {
            for (const auto &choice : value["choices"]) {
                if (!choice.is_object() || !choice.contains("delta") ||
                    !choice["delta"].is_object())
                    continue;
                for (auto it = choice["delta"].begin();
                     it != choice["delta"].end(); ++it) {
                    if (it.key() != "role" && nonempty_value(it.value()))
                        return true;
                }
            }
        }

        std::string type = event_name;
        if (value.contains("type") && value["type"].is_string())
            type = value["type"].get<std::string>();
        if (type == "content_block_delta" && value.contains("delta") &&
            value["delta"].is_object()) {
            const auto &delta = value["delta"];
            // Anthropic signatures are terminal metadata, not generated
            // content. Unknown non-empty delta shapes are semantic.
            if (delta.contains("type") && delta["type"].is_string() &&
                delta["type"].get_ref<const std::string &>() ==
                    "signature_delta")
                return false;
            for (auto it = delta.begin(); it != delta.end(); ++it) {
                if (it.key() != "type" && nonempty_value(it.value()))
                    return true;
            }
        }

        // Responses extensions consistently name incremental model output
        // with a .delta suffix (audio, image, future modalities).
        return (has_suffix(type, ".delta") || has_suffix(type, "_delta")) &&
               type != "signature_delta" &&
               value.contains("delta") && nonempty_value(value["delta"]);
    }

    fmt::SseFrameBuffer frames_;
};

/// Build a JSON error response.
static std::string json_error(const std::string &msg, int code) {
    json j;
    const char *type = code == 401 || code == 403 ? "auth_error"
                     : code == 429 ? "rate_limit_error"
                     : code >= 500 ? "upstream_error"
                                   : "invalid_request_error";
    j["error"] = {{"message", msg}, {"type", type}, {"code", code}};
    return j.dump();
}

/// Unified upstream-timeout error object returned to the client.
/// type=timeout_error + code 504 — clients (opencode, Claude Code, SDK retry
/// middlewares) recognize this as a retryable error instead of an opaque
/// connection drop.  `timeout_secs` is the configured value that fired.
static json timeout_error_body(int timeout_secs = 0) {
    std::string msg = timeout_secs > 0
        ? "Upstream timeout: no response within " +
              std::to_string(timeout_secs) + "s. Please retry."
        : "Upstream timeout: no response within the configured "
          "timeout. Please retry.";
    return json{{"message", msg},
                {"type", "timeout_error"},
                {"code", 504}};
}

static int stream_error_status(const json &error) {
    if (error.is_object()) {
        for (const char *field : {"status", "status_code", "code"}) {
            if (error.contains(field) && error[field].is_number_integer()) {
                const int code = error[field].get<int>();
                if (code >= 400 && code <= 599) return code;
            }
        }
        std::string classification;
        for (const char *field : {"type", "code"}) {
            if (error.contains(field) && error[field].is_string())
                classification += " " + error[field].get<std::string>();
        }
        std::transform(classification.begin(), classification.end(),
                       classification.begin(), [](unsigned char c) {
                           return static_cast<char>(std::tolower(c));
                       });
        if (classification.find("rate") != std::string::npos ||
            classification.find("quota") != std::string::npos)
            return 429;
        if (classification.find("auth") != std::string::npos ||
            classification.find("api_key") != std::string::npos)
            return 401;
        if (classification.find("permission") != std::string::npos)
            return 403;
        if (classification.find("overload") != std::string::npos)
            return 503;
    }
    return 502;
}

static std::string stream_error_message(const json &error) {
    if (error.is_object() && error.contains("message") &&
        error["message"].is_string())
        return error["message"].get<std::string>();
    if (error.is_string()) return error.get<std::string>();
    return "Upstream emitted an in-stream error";
}

// ── Auth / routing helpers ───────────────────────────────────────────────

/// Result of extracting Bearer token + looking up route.
struct AuthResult {
    bool success = false;
    Router::RouteResult route;   // valid only if success
    std::string error_json;       // 401 JSON body when !success
};

/// Resolve the upstream request path from the account config.
/// - If `endpoint_path` is set, it is used verbatim (path_is_full = true),
///   bypassing the base_url path component — fixes the /v1 double-append.
/// - Otherwise the path is derived from api_format and appended to the
///   base_url path (legacy behavior preserved).
static void resolve_upstream_path(const std::string &api_format,
                                  const std::string &base_url,
                                  const std::string &endpoint_path,
                                  std::string &out_path,
                                  bool &out_path_is_full) {
    if (!endpoint_path.empty()) {
        out_path = endpoint_path;
        out_path_is_full = true;
        return;
    }
    out_path_is_full = false;

    // Extract the base_url path component to detect a trailing "/v1"
    // (OpenAI-style base URLs) and avoid "/v1/v1/messages" double-append.
    std::string base_path;
    size_t scheme_end = base_url.find("://");
    if (scheme_end != std::string::npos) {
        scheme_end += 3;
        size_t path_start = base_url.find('/', scheme_end);
        if (path_start != std::string::npos)
            base_path = base_url.substr(path_start);
    }

    if (api_format == "anthropic") {
        if (base_path.size() >= 3 &&
            base_path.compare(base_path.size() - 3, 3, "/v1") == 0)
            out_path = "/messages";
        else
            out_path = "/v1/messages";
    } else if (api_format == "openai_responses") {
        out_path = "/responses";
    } else {
        out_path = "/chat/completions";
    }
}

/// Extract Bearer token from request and look up the upstream route.
/// Returns 401 error info when authentication or routing fails.
static AuthResult extract_and_route(const httplib::Request &req,
                                     Router &router) {
    AuthResult ar;
    std::string local_key;
    // Accept both `Authorization: Bearer <key>` (OpenAI clients, cc with
    // ANTHROPIC_AUTH_TOKEN) and `x-api-key: <key>` (Anthropic SDK clients with
    // ANTHROPIC_API_KEY).  The inbound scheme is irrelevant to routing — the
    // key only selects the upstream account.
    if (req.has_header("Authorization")) {
        std::string auth = req.get_header_value("Authorization");
        if (auth.rfind("Bearer ", 0) == 0)
            local_key = auth.substr(7);
    }
    if (local_key.empty() && req.has_header("x-api-key"))
        local_key = req.get_header_value("x-api-key");

    if (local_key.empty()) {
        ar.error_json = json_error("Missing API key. "
                                    "Use: Authorization: Bearer <key>", 401);
        return ar;
    }

    ar.route = router.route(local_key);
    if (!ar.route.success) {
        ar.error_json = json_error(ar.route.error, 401);
        return ar;
    }

    ar.success = true;
    return ar;
}

// ── Model helpers ────────────────────────────────────────────────────────

/// Apply the effective upstream model to a raw request body in place
/// (best-effort: JSON rewrite first, substring fallback otherwise).
static void apply_body_model(std::string &body, const std::string &model) {
    try {
        json j = json::parse(body);
        if (j.contains("model") && j["model"].is_string() &&
            j["model"].get<std::string>() != model) {
            j["model"] = model;
            body = j.dump();
        }
    } catch (...) {
        size_t pos = body.find("\"model\"");
        if (pos != std::string::npos) {
            auto colon = body.find(':', pos);
            auto q1 = body.find('"', colon + 1);
            auto q2 = body.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos)
                body.replace(q1 + 1, q2 - q1 - 1, model);
        }
    }
}

/// Resolve the effective upstream targets for a request: strip the Claude
/// Code `[1m]`/`[1M]` context-window marker (upstreams reject it), then —
/// for aggregate accounts — collect every entry matching the model as a
/// candidate (priority = sort_order, id).  Plain accounts yield one
/// candidate.  Returns an empty list when an aggregate has no match.
/// Expand one real account into candidates — one per configured key slot
/// (ordered by position,id) or a single legacy candidate using the account's
/// upstream_key when it has no upstream_keys rows.  `model` is the model name
/// forwarded upstream.
static void push_account_candidates(std::vector<UpstreamCandidate> &cands,
                                    const Database::AccountInfo &acct,
                                    const std::vector<Database::KeySlot> &keys,
                                    const std::string &model,
                                    int priority_group) {
    if (keys.empty()) {
        // Legacy single-key fallback: use the account's upstream_key as a
        // single candidate.  Concurrency slot = -account_id (negative, unique
        // per account — never collides with real upstream_keys.id which is
        // always positive), so legacy accounts keep independent budgets.
        // An account whose multi-key set was emptied also has an empty legacy
        // value; it is unavailable, not a candidate with a blank credential.
        if (acct.upstream_key.empty()) return;
        UpstreamCandidate c;
        c.account = acct;
        c.key = acct.upstream_key;
        c.key_slot_id = -acct.id;
        c.upstream_model = model;
        c.priority_group = priority_group;
        cands.push_back(std::move(c));
    } else {
        // One candidate per key slot, same account config, ordered by
        // (position, id) so overflow follows a fixed fill order.
        for (const auto &k : keys) {
            if (k.key_value.empty()) continue;
            UpstreamCandidate c;
            c.account = acct;          // keys share the account config
            c.key = k.key_value;
            c.key_slot_id = k.id;
            c.upstream_model = model;
            c.priority_group = priority_group;
            cands.push_back(std::move(c));
        }
    }
}

/// Resolve the effective upstream targets for a request: strip the Claude
/// Code `[1m]`/`[1M]` context-window marker (upstreams reject it), then —
/// for aggregate accounts — collect every entry matching the model, each
/// member account expanded into its key slots (priority = sort_order, id).
/// Plain accounts yield one candidate per key slot.  Returns an empty list
/// when an aggregate has no match.
static std::vector<UpstreamCandidate>
resolve_candidates_uncached(Database &db, const Router::RouteResult &route,
                            std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    std::vector<UpstreamCandidate> cands;
    // Account configuration, aggregate mappings and key slots come from one
    // SQLite statement snapshot. Never combine a newly rotated credential
    // with a base URL read before the same config transaction committed.
    auto targets = db.resolve_routing_snapshot(route.account_id, model);
    for (const auto &target : targets) {
        if (target.account.deleted) continue;
        if (route.is_aggregate) {
            fprintf(stderr,
                    "[Proxy] aggregate %d: %s → account %d (%s) model %s\n",
                    route.account_id, model.c_str(), target.account.id,
                    target.account.name.c_str(), target.upstream_model.c_str());
        }
        push_account_candidates(cands, target.account, target.keys,
                                target.upstream_model,
                                target.priority_group);
    }
    return cands;
}

void ProxyServer::enqueue_log(
    int account_id, int local_key_id, const UsageTracker::UsageInfo &usage,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int ttft_ms, int generation_ms, double output_tps, int upstream_ttft_ms,
    int upstream_duration_ms, int attempt_count,
    const std::vector<Database::AttemptInfo> &attempts) {
    LogJob job;
    job.account_id = account_id;
    job.local_key_id = local_key_id;
    job.usage = usage;
    job.is_streaming = is_streaming;
    job.status_code = status_code;
    job.duration_ms = duration_ms;
    job.upstream_key_id = upstream_key_id;
    job.ttft_ms = ttft_ms;
    job.generation_ms = generation_ms;
    job.output_tps = output_tps;
    job.upstream_ttft_ms = upstream_ttft_ms;
    job.upstream_duration_ms = upstream_duration_ms;
    job.attempt_count = attempt_count;
    job.attempts = attempts;

    std::call_once(accounting_thread_once_, [this] {
        accounting_thread_ = std::thread(&ProxyServer::accounting_loop, this);
    });

    bool dropped = false;
    {
        std::lock_guard<std::mutex> lock(accounting_mutex_);
        if (accounting_stop_) return;  // shutdown — drop silently
        if (accounting_queue_.size() >= kAccountingQueueMax) {
            dropped = true;
        } else {
            accounting_queue_.push_back(std::move(job));
        }
    }
    if (dropped) {
        const std::uint64_t n =
            accounting_dropped_.fetch_add(1, std::memory_order_relaxed) + 1;
        if (n == 1 || n % 1000 == 0)
            fprintf(stderr,
                    "[Proxy] accounting queue saturated; dropped %llu "
                    "request-log entries\n",
                    static_cast<unsigned long long>(n));
        return;
    }
    accounting_cv_.notify_one();
}

void ProxyServer::accounting_loop() {
    for (;;) {
        std::vector<LogJob> batch;
        {
            std::unique_lock<std::mutex> lock(accounting_mutex_);
            accounting_cv_.wait(lock, [&] {
                return accounting_stop_ || !accounting_queue_.empty();
            });
            if (accounting_stop_ && accounting_queue_.empty()) return;
            batch.reserve(accounting_queue_.size());
            while (!accounting_queue_.empty()) {
                batch.push_back(std::move(accounting_queue_.front()));
                accounting_queue_.pop_front();
            }
        }
        for (const auto &job : batch) {
            try {
                double cost = 0.0;
                tracker_.log_request(
                    job.account_id, job.local_key_id, job.usage,
                    job.is_streaming, job.status_code, job.duration_ms,
                    job.upstream_key_id, job.ttft_ms, job.generation_ms,
                    job.output_tps, job.upstream_ttft_ms,
                    job.upstream_duration_ms, job.attempt_count,
                    job.attempts, &cost);
                // Feed the cold-start cost ledger so new sessions prefer the
                // key slot that has spent least.  `upstream_key_id` is the
                // slot that actually served the request (used->key_slot_id).
                if (job.upstream_key_id != 0 && cost > 0.0)
                    cost_ledger_.add(job.upstream_key_id, cost);
            } catch (const std::exception &e) {
                fprintf(stderr,
                        "[Proxy] accounting log error (account=%d status=%d): %s\n",
                        job.account_id, job.status_code, e.what());
            } catch (...) {
                fprintf(stderr,
                        "[Proxy] accounting log error (account=%d status=%d)\n",
                        job.account_id, job.status_code);
            }
        }
    }
}

std::vector<UpstreamCandidate> ProxyServer::resolve_candidates_cached(
    const Router::RouteResult &route, std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    const std::string cache_key = std::to_string(route.account_id) + "\x1f" + model;
    {
        std::unique_lock<std::mutex> lock(candidate_cache_mutex_);
        for (;;) {
            const auto now = std::chrono::steady_clock::now();
            auto it = candidate_cache_.find(cache_key);
            if (it != candidate_cache_.end() && now < it->second.expires_at)
                return it->second.candidates;
            if (it != candidate_cache_.end()) candidate_cache_.erase(it);

            if (candidate_cache_loading_.insert(cache_key).second) break;
            candidate_cache_cv_.wait(lock, [&] {
                return candidate_cache_loading_.count(cache_key) == 0;
            });
            // The loader has published (or failed to publish) the value. Check
            // the cache again before electing another loader.
        }
    }

    std::vector<UpstreamCandidate> candidates;
    try {
        candidates = resolve_candidates_uncached(db_, route, model);
    } catch (...) {
        {
            std::lock_guard<std::mutex> lock(candidate_cache_mutex_);
            candidate_cache_loading_.erase(cache_key);
        }
        candidate_cache_cv_.notify_all();
        throw;
    }
    {
        std::lock_guard<std::mutex> lock(candidate_cache_mutex_);
        // Bounded cache: evict expired/arbitrary entries incrementally instead
        // of clearing the whole map and stampeding every hot route at once.
        const auto completed_at = std::chrono::steady_clock::now();
        for (auto it = candidate_cache_.begin();
             it != candidate_cache_.end() && candidate_cache_.size() >= 1024;) {
            if (completed_at >= it->second.expires_at)
                it = candidate_cache_.erase(it);
            else
                ++it;
        }
        while (candidate_cache_.size() >= 1024)
            candidate_cache_.erase(candidate_cache_.begin());
        candidate_cache_[cache_key] = CandidateCacheEntry{
            candidates, completed_at + std::chrono::seconds(1)};
        candidate_cache_loading_.erase(cache_key);
    }
    candidate_cache_cv_.notify_all();
    return candidates;
}

/// Pick a candidate starting at `start` and advancing in cyclic order
/// (start, start+1, …, n-1, 0, …, start-1).  Concurrency and plan cooldown
/// are both tracked per key slot.  Returns the picked index via `picked`.
[[maybe_unused]] static bool pick_candidate(AccountGate &gate,
                           const std::vector<UpstreamCandidate> &cands,
                           size_t start, size_t &picked) {
    size_t n = cands.size();
    for (size_t i = 0; i < n; ++i) {
        size_t idx = (start + i) % n;
        const auto &c = cands[idx];
        if (gate.in_cooldown(c.key_slot_id)) {
            fprintf(stderr, "[Proxy] key slot %d (account %d %s) cooling "
                            "down, skip\n",
                    c.key_slot_id, c.account.id, c.account.name.c_str());
            continue;
        }
        if (!gate.acquire(c.key_slot_id, c.account.max_concurrency)) {
            fprintf(stderr, "[Proxy] key slot %d (account %d %s) at "
                            "concurrency limit (%d), try next\n",
                    c.key_slot_id, c.account.id, c.account.name.c_str(),
                    c.account.max_concurrency);
            continue;
        }
        picked = idx;
        return true;
    }
    return false;
}

// ── add_cors_headers ─────────────────────────────────────────────────────

void ProxyServer::add_cors_headers(httplib::Response &res) {
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.set_header("Access-Control-Allow-Headers",
                   "Authorization, Content-Type, X-API-Key, X-Session-ID, "
                   "X-Conversation-ID");
}

// ── Format resolution helpers ────────────────────────────────────────────

/// Resolve the harness (client-side) format from the incoming request URL path.
/// Each chat endpoint has a canonical wire format:
///   /v1/chat/completions → OpenAI, /v1/responses → OpenAI Responses,
///   /v1/messages → Anthropic.
///
/// A client whose base URL already ends in `/v1` (e.g.
/// `ANTHROPIC_BASE_URL=http://host:8800/v1`) appends the endpoint again,
/// producing `/v1/v1/messages`.  Tolerate that double `/v1` prefix (mirrors
/// cc-switch, which registers `/v1/v1/chat/completions` etc.).
static ir::ApiFormat harness_format_from_path(const std::string &path) {
    std::string p = path;
    if (p.rfind("/v1/v1/", 0) == 0) p = p.substr(3);  // "/v1/v1/…" → "/v1/…"
    if (p == "/v1/responses") return ir::ApiFormat::OpenAIResponses;
    if (p == "/v1/messages") return ir::ApiFormat::Anthropic;
    return ir::ApiFormat::OpenAI;  // "/v1/chat/completions" (default)
}

/// Resolved upstream target (path + auth/path options) for a route.
struct UpstreamTarget {
    std::string path;
    ForwardOptions opts;
};
static UpstreamTarget resolve_upstream_target(const std::string &api_format,
                                              const std::string &base_url,
                                              const std::string &endpoint_path,
                                              const std::string &auth_header,
                                              const Database::TimeoutConfig &tc) {
    UpstreamTarget t;
    resolve_upstream_path(api_format, base_url, endpoint_path, t.path,
                          t.opts.path_is_full);
    // Outbound auth scheme: `auto` (the dashboard default) derives from the
    // upstream wire format — Anthropic-native uses x-api-key + anthropic-version,
    // everything else uses Authorization: Bearer.  Explicit `bearer` /
    // `x-api-key` remain as overrides for relays that need them.
    if (auth_header == "auto" || auth_header.empty())
        t.opts.auth_scheme = (api_format == "anthropic") ? "x-api-key" : "bearer";
    else
        t.opts.auth_scheme = auth_header;
    t.opts.streaming_first_byte_timeout = tc.streaming_first_byte_timeout;
    t.opts.streaming_semantic_timeout = tc.streaming_first_byte_timeout;
    t.opts.streaming_idle_timeout = tc.streaming_idle_timeout;
    t.opts.non_streaming_timeout = tc.non_streaming_timeout;
    t.opts.non_streaming_total_timeout = tc.non_streaming_timeout;
    return t;
}

/// Per-format timeout config for a client request, mirroring cc-switch's
/// per-app-type timeouts keyed here by the client's wire format.
Database::TimeoutConfig ProxyServer::timeout_config_cached(
    ir::ApiFormat harness) {
    const int key = static_cast<int>(harness);
    std::lock_guard<std::mutex> lock(timeout_cache_mutex_);
    const auto now = std::chrono::steady_clock::now();
    auto &entry = timeout_cache_[key];
    if (entry.valid && now < entry.expires_at) return entry.config;

    switch (harness) {
        case ir::ApiFormat::Anthropic:
            entry.config = db_.get_timeout_config("anthropic");
            break;
        case ir::ApiFormat::OpenAIResponses:
            entry.config = db_.get_timeout_config("openai_responses");
            break;
        default:  // OpenAI chat
            entry.config = db_.get_timeout_config("openai");
            break;
    }
    entry.valid = true;
    entry.expires_at = std::chrono::steady_clock::now() +
                       std::chrono::seconds(1);
    return entry.config;
}

std::uint64_t ProxyServer::request_started(const std::string &model,
                                           bool streaming) {
    std::uint64_t id = next_request_id_.fetch_add(1, std::memory_order_relaxed);
    // Zero is the invalid sentinel. The wrap can only occur after 2^64-1
    // requests, but handle it without leaking the live-request entry.
    if (id == 0)
        id = next_request_id_.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lock(live_requests_mutex_);
        live_requests_.emplace(id, LiveRequest{
            model, streaming, std::chrono::steady_clock::now()});
    }
    in_flight_count_.fetch_add(1, std::memory_order_relaxed);
    return id;
}

void ProxyServer::request_finished(std::uint64_t request_id) {
    if (request_id == 0) return;
    bool removed = false;
    {
        std::lock_guard<std::mutex> lock(live_requests_mutex_);
        removed = live_requests_.erase(request_id) != 0;
    }
    if (removed) in_flight_count_.fetch_sub(1, std::memory_order_relaxed);
}

/// Retries share one pre-response budget.  Without this clamp, N unhealthy
/// keys each consume the full configured timeout and turn a 60s request into
/// an N×60s stall.  A successfully committed stream is not cut off here; its
/// normal idle timeout continues to protect the established stream.
static bool clamp_to_remaining_budget(Database::TimeoutConfig &tc,
                                      std::chrono::steady_clock::time_point deadline,
                                      bool streaming) {
    auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now()).count();
    if (remaining <= 0) return false;
    const int seconds = std::max(1, static_cast<int>((remaining + 999) / 1000));
    int &field = streaming ? tc.streaming_first_byte_timeout
                           : tc.non_streaming_timeout;
    field = field > 0 ? std::min(field, seconds) : seconds;
    return true;
}

/// Non-streaming usage parser dispatcher by upstream api_format.
static std::optional<UsageTracker::UsageInfo>
parse_usage_for_format(const std::string &api_format, const std::string &body) {
    if (api_format == "anthropic") return UsageTracker::parse_anthropic_usage(body);
    if (api_format == "openai_responses") return UsageTracker::parse_responses_usage(body);
    return UsageTracker::parse_usage(body);
}

/// Convert IR usage into UsageInfo. Anthropic codecs keep cache tokens
/// separate from input_tokens, while OpenAI/Responses prompt_tokens already
/// include cache hits — so for Anthropic upstreams the cache tokens are
/// folded into prompt_tokens (matching parse_anthropic_usage semantics),
/// making `prompt - cache_read` the uncached input for every path.
static UsageTracker::UsageInfo usage_from_ir(const ir::Usage &u,
                                             ir::ApiFormat upstream_fmt) {
    UsageTracker::UsageInfo info;
    info.prompt_tokens = u.prompt_tokens;
    info.completion_tokens = u.completion_tokens;
    info.cache_read_tokens = u.cache_read_tokens;
    info.cache_creation_tokens = u.cache_creation_tokens;
    if (upstream_fmt == ir::ApiFormat::Anthropic)
        info.prompt_tokens += u.cache_read_tokens + u.cache_creation_tokens;
    info.total_tokens = u.total_tokens;
    return info;
}

/// Check whether the client disconnected while we waited for upstream.
static bool client_disconnected(const httplib::Request &req,
                                std::uint64_t inflight_id,
                                const std::string &model) {
    if (req.client_socket == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = req.client_socket;
    // POLLRDHUP only means that the peer closed its write half. A client may
    // legitimately send one complete request, shutdown(SHUT_WR), and keep
    // reading the response, so only hard socket errors count as disconnects.
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, 0) > 0 &&
        (pfd.revents & (POLLHUP | POLLERR | POLLNVAL))) {
        fprintf(stderr, "[Proxy] Client gone, drop response "
                        "(inflight=%llu, model=%s)\n",
                static_cast<unsigned long long>(inflight_id), model.c_str());
        return true;
    }
    return false;
}

/// True if the client socket is already closed.  Used as a final race-safe
/// check after the process-wide watchdog has cancelled an upstream request.
static bool client_socket_gone(int sock) {
    if (sock == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = sock;
    pfd.events = POLLIN;
    pfd.revents = 0;
    return poll(&pfd, 1, 0) > 0 &&
           (pfd.revents & (POLLHUP | POLLERR | POLLNVAL));
}

/// Synchronous non-streaming forward.  UpstreamClient's process-wide watchdog
/// monitors the downstream socket without creating a per-request thread.
static UpstreamClient::ForwardResult
forward_once(UpstreamClient &upstream, const std::string &body,
             const std::string &content_type, const UpstreamCandidate &c,
             const UpstreamTarget &target, int client_sock) {
    auto opts = target.opts;
    opts.downstream_socket = client_sock;
    return upstream.forward(
        "POST", c.account.base_url, c.key,
        target.path, body, content_type, nullptr, opts);
}

static Database::AttemptInfo attempt_info(
    const UpstreamCandidate &candidate,
    const UpstreamClient::ForwardResult &result,
    int semantic_ttft_ms = -1) {
    Database::AttemptInfo out;
    out.account_id = candidate.account.id;
    out.upstream_key_id = candidate.key_slot_id;
    out.status_code = result.status_code;
    out.duration_ms = result.duration_ms;
    out.ttft_ms = semantic_ttft_ms;
    out.is_timeout = result.is_timeout;
    out.error = result.error;
    return out;
}

// ── handle_chat_request ──────────────────────────────────────────────────

/// Entry point for /v1/chat/completions, /v1/messages and /v1/responses.
/// The harness format comes from the incoming request URL path; when it matches
/// the account's api_format we use the passthrough fast path, otherwise we
/// convert via the IR codecs.
///
/// Candidate handling: plain accounts have one candidate; aggregate accounts
/// may have several (same model → several upstreams, priority order).  The
/// first candidate that is not cooling down and has a free concurrency slot
/// wins.  Non-streaming requests fall back to the next candidate when the
/// upstream answers 429 (plan accounts then cool down for 5h) or 5xx before
/// anything was sent to the client.  Streaming requests are one-shot: once
/// the chunked provider starts, headers are committed, so no fallback.
void ProxyServer::handle_chat_request(const httplib::Request &req,
                                      httplib::Response &res) {
    add_cors_headers(res);
    auto t0 = std::chrono::steady_clock::now();

    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    // Aggregate accounts need the request model to pick the real upstream
    // account, so resolution happens here — before the passthrough/converted
    // split.  For plain accounts this only strips the `[1m]`/`[1M]` marker.
    std::string model = extract_model(req.body);
    auto cands = resolve_candidates_cached(ar.route, model);
    if (cands.empty()) {
        res.status = 400;
        res.set_content(json_error("Model '" + model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }

    ir::ApiFormat harness = harness_format_from_path(req.path);
    bool is_stream = is_streaming_request(req.body);

    // ── Streaming: defer candidate selection into the provider ─────────
    // The chunked response headers are intentionally not committed until the
    // provider writes its first event.  This lets handle_streaming try the
    // next key when an upstream returns 429/5xx before emitting any bytes.
    if (is_stream) {
        // Validate the harness request BEFORE acquiring a gate slot: a
        // 400 early-return must not leak the concurrency slot.
        const FormatCodec &hc = codecs_.get(harness);
        ir::ChatRequest cReqCheck;
        std::string perr;
        json req_json;
        try {
            req_json = json::parse(req.body);
        } catch (...) {
            res.status = 400;
            res.set_content(hc.serialize_error_body(
                json{{"message", "invalid JSON body"}, {"type", "parse_error"}}).dump(),
                "application/json");
            return;
        }
        if (!hc.parse_request(req_json, cReqCheck, perr)) {
            res.status = 400;
            res.set_content(hc.serialize_error_body(
                json{{"message", perr}, {"type", "parse_error"}}).dump(),
                "application/json");
            return;
        }

        std::string session_id = extract_session_id(req, req_json);
        const auto scope = affinity_scope(ar.route.local_key_id, harness);
        size_t start = affinity_start(affinity_, scope, session_id, cands);
        handle_streaming(cands, start, session_id, ar.route.local_key_id,
                         harness, model, req, res, t0);
        return;
    }

    // ── Non-streaming: candidate loop with fallback ────────────────────
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    // Parse the harness request once, up front (converted path needs it).
    const FormatCodec &harness_codec = codecs_.get(harness);
    ir::ChatRequest cReq;
    std::string perr;
    json req_json;
    try {
        req_json = json::parse(req.body);
    } catch (...) {
        res.status = 400;
        res.set_content(harness_codec.serialize_error_body(
            json{{"message", "invalid JSON body"}, {"type", "parse_error"}}).dump(),
            "application/json");
        return;
    }
    if (!harness_codec.parse_request(req_json, cReq, perr)) {
        res.status = 400;
        res.set_content(harness_codec.serialize_error_body(
            json{{"message", perr}, {"type", "parse_error"}}).dump(),
            "application/json");
        return;
    }

    std::uint64_t inflight_id = 0;
    auto inflight_guard = make_scope_exit(
        [this, &inflight_id] { request_finished(inflight_id); });
    int concurrent_count = 0;
    UpstreamClient::ForwardResult fwd;
    const UpstreamCandidate *used = nullptr;
    const UpstreamCandidate *last_attempted = nullptr;
    std::vector<Database::AttemptInfo> attempts;
    bool think_filter = false;
    ir::ApiFormat used_upstream_fmt = harness;
    const FormatCodec *upstream_codec = nullptr;
    std::optional<ir::ChatResponse> converted_response;

    // Session-affinity spillover: start at the session's preferred candidate
    // and wrap around in fixed order (P, P+1, …, n-1, 0, …, P-1).
    std::string session_id = extract_session_id(req, req_json);
    const auto scope = affinity_scope(ar.route.local_key_id, harness);
    size_t start = affinity_start(affinity_, scope, session_id, cands);
    const auto order = candidate_order(
        cands, start, routing_rr_.fetch_add(1, std::memory_order_relaxed));
    int attempts_made = 0;
    const auto base_timeouts = timeout_config_cached(harness);
    const int budget_seconds = base_timeouts.non_streaming_timeout > 0
        ? base_timeouts.non_streaming_timeout : 600;
    const auto deadline = t0 + std::chrono::seconds(budget_seconds);

    for (size_t i = 0; i < order.size(); ++i) {
        size_t ci = order[i];
        const UpstreamCandidate &c = cands[ci];
        if (!gate_.try_acquire_eligible(c.key_slot_id,
                                        c.account.max_concurrency)) continue;
        GateLease gate_lease(gate_, c.key_slot_id);

        if (inflight_id == 0) {
            inflight_id = request_started(c.upstream_model, false);
            concurrent_count = in_flight_count();
        }

        ir::ApiFormat upstream = ir::parse_api_format(c.account.api_format);
        auto attempt_timeouts = base_timeouts;
        if (!clamp_to_remaining_budget(attempt_timeouts, deadline, false)) {
            fwd.status_code = 504;
            fwd.is_timeout = true;
            fwd.timeout_secs = budget_seconds;
            fwd.error = "request retry budget exhausted";
            break;
        }
        ++attempts_made;
        auto target = resolve_upstream_target(
            c.account.api_format, c.account.base_url,
            c.account.endpoint_path, c.account.auth_header,
            attempt_timeouts);

        fprintf(stderr, "[Proxy] %s %s request from key_id=%d to account=%d "
                        "model=%s (concurrent=%d, inflight_id=%llu)\n",
                ir::to_string(harness).c_str(),
                (harness == upstream) ? "passthrough" : "convert",
                ar.route.local_key_id, c.account.id,
                c.upstream_model.c_str(), concurrent_count,
                static_cast<unsigned long long>(inflight_id));

        if (harness == upstream) {
            std::string body = req.body;
            apply_body_model(body, c.upstream_model);
            fwd = forward_once(upstream_, body, content_type, c, target,
                               req.client_socket);
            think_filter = (upstream == ir::ApiFormat::OpenAI);
            converted_response.reset();
        } else {
            cReq.model = c.upstream_model;
            upstream_codec = &codecs_.get(upstream);
            std::string body = upstream_codec->serialize_request(cReq).dump();
            fwd = forward_once(upstream_, body, "application/json", c, target,
                               req.client_socket);
            think_filter = false;
            converted_response.reset();
        }
        last_attempted = &c;
        used_upstream_fmt = upstream;

        // A 2xx body in the wrong protocol is a failed candidate, not a
        // successful response that can be forwarded verbatim to the harness.
        if (harness != upstream && fwd.success &&
            fwd.status_code >= 200 && fwd.status_code < 300) {
            ir::ChatResponse parsed;
            bool parsed_ok = false;
            perr.clear();
            try {
                parsed_ok = upstream_codec->parse_response(json::parse(fwd.body),
                                                            parsed, perr);
            } catch (const std::exception &e) {
                perr = e.what();
            } catch (...) {
                perr = "unknown response conversion error";
            }
            if (parsed_ok) {
                converted_response = std::move(parsed);
            } else {
                fwd.success = false;
                fwd.status_code = 502;
                fwd.error = "Invalid upstream response for configured format";
                if (!perr.empty()) fwd.error += ": " + perr;
            }
        }
        attempts.push_back(attempt_info(c, fwd));

        const bool downstream_gone = fwd.client_disconnected ||
                                     client_socket_gone(req.client_socket);
        if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300)
            gate_.mark_success(c.key_slot_id);
        else if (!downstream_gone &&
                 candidate_failure_retryable(fwd.status_code))
            gate_.record_failure(c.key_slot_id,
                                 account_types::cooldown_class(c.account.account_type),
                                 fwd.usage_limit, fwd.status_code);
        // Publish success/cooldown before making the slot acquirable, otherwise
        // a sibling request can race into a key whose 401/429/5xx is already
        // known. Parsing is complete; logging/client writes stay outside Gate.
        gate_lease.release();

        // Credential failures are key-local just like rate limits. Fall back
        // before returning 401/403 when a sibling key can still serve.
        bool retryable = !downstream_gone &&
                         candidate_failure_retryable(fwd.status_code) &&
                         i + 1 < order.size();
        if (retryable) {
            fprintf(stderr, "[Proxy] upstream %d (%s) failed (%d), trying "
                            "next candidate\n",
                    c.account.id, c.account.name.c_str(), fwd.status_code);
            continue;
        }
        used = &c;
        break;
    }

    // Bind only on a successful response below. Failed keys must never become
    // the session's next preferred route.

    if (!used) {
        inflight_guard.run_now();
        UsageTracker::UsageInfo zero;
        zero.model = model;
        const int final_status = fwd.is_timeout ? 504
            : (!attempts.empty() && fwd.status_code >= 400
                ? fwd.status_code : 429);
        enqueue_log(last_attempted ? last_attempted->account.id
                                            : ar.route.account_id,
                             ar.route.local_key_id, zero,
                             false, final_status,
                             static_cast<int>(std::chrono::duration_cast<
                                 std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count()),
                             last_attempted ? last_attempted->key_slot_id : 0,
                             -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
        if (fwd.is_timeout) {
            // Every candidate timed out (the last forward was a timeout) — a
            // timeout error, not a busy signal.
            res.status = 504;
            res.set_header("Connection", "close");
            res.set_content(harness_codec.serialize_error_body(
                                timeout_error_body(fwd.timeout_secs)).dump(),
                            "application/json");
        } else if (attempts.empty()) {
            res.status = 429;
            res.set_content(harness_codec.serialize_error_body(json{
                                {"message", "All upstream accounts are busy, "
                                            "cooling down, or failed"},
                                {"type", "rate_limit_error"},
                                {"code", 429}}).dump(),
                            "application/json");
        } else {
            res.status = final_status;
            json normalized;
            try {
                normalized = codecs_.get(used_upstream_fmt).parse_error_body(
                    json::parse(fwd.body));
            } catch (...) {
                normalized = json{
                    {"message", fwd.error.empty() ? "upstream error"
                                                   : fwd.error},
                    {"type", "upstream_error"},
                    {"code", final_status}};
            }
            res.set_content(harness_codec.serialize_error_body(normalized).dump(),
                            "application/json");
        }
        return;
    }

    if (fwd.client_disconnected || client_disconnected(req, inflight_id, model)) {
        // Record the aborted request truthfully (client closed before we
        // could send a response): status 499, zero tokens.
        UsageTracker::UsageInfo zu;
        zu.model = model;
        int dur = static_cast<int>(std::chrono::duration_cast<
            std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0)
                .count());
        inflight_guard.run_now();
        enqueue_log(used->account.id, ar.route.local_key_id, zu,
                             false, 499, dur, used->key_slot_id,
                             -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));
    inflight_guard.run_now();

    // ── Non-streaming response handling (passthrough vs converted) ──
    if (used_upstream_fmt == harness) {
        if (fwd.success) {
            auto usage = parse_usage_for_format(ir::to_string(used_upstream_fmt),
                                                fwd.body);
            if (usage.has_value()) {
                enqueue_log(used->account.id, ar.route.local_key_id,
                                     *usage, false, fwd.status_code,
                                     fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
            } else {
                fprintf(stderr, "[Proxy] Warning: could not parse usage "
                                "from non-streaming response, model=%s\n",
                        model.c_str());
                UsageTracker::UsageInfo zu;
                zu.model = model;
                enqueue_log(used->account.id, ar.route.local_key_id, zu,
                                     false, fwd.status_code, fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
            }
            if (think_filter)
                res.set_content(sanitize_response_body(fwd.body),
                                "application/json");
            else
                res.set_content(fwd.body, "application/json");
            res.status = fwd.status_code;
        } else {
            // Upstream error / timeout: record the failed attempt (zero
            // tokens, truthful status — 504 on timeout, else upstream code).
            UsageTracker::UsageInfo zu;
            zu.model = model;
            enqueue_log(used->account.id, ar.route.local_key_id, zu,
                                 false, fwd.status_code, fwd.duration_ms, used->key_slot_id,
                                 -1, -1, -1.0, -1, -1, attempts_made, attempts);
            res.status = fwd.status_code;
            if (fwd.is_timeout) {
                res.set_header("Connection", "close");
                res.set_content(harness_codec.serialize_error_body(
                                    timeout_error_body(fwd.timeout_secs)).dump(),
                                "application/json");
            } else if (!fwd.body.empty()) {
                // Passthrough means the upstream body already uses the
                // client's protocol. Preserve its structured error details.
                res.set_content(fwd.body, "application/json");
            } else {
                res.set_content(harness_codec.serialize_error_body(json{
                                    {"message", fwd.error.empty()
                                                    ? "upstream error"
                                                    : fwd.error},
                                    {"type", "upstream_error"},
                                    {"code", fwd.status_code}}).dump(),
                                "application/json");
            }
        }
    } else {
        if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
            if (converted_response.has_value()) {
                const auto &cResp = *converted_response;
                auto usage_info = usage_from_ir(cResp.usage, used_upstream_fmt);
                usage_info.model = model;
                enqueue_log(used->account.id, ar.route.local_key_id,
                                     usage_info, false,
                                     fwd.status_code, fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
                std::string outgoing_body = harness_codec.serialize_response(cResp).dump();
                if (harness == ir::ApiFormat::OpenAIResponses)
                    affinity_.bind(scope, response_id_from_body(outgoing_body),
                                   used->key_slot_id);
                res.status = fwd.status_code;
                res.set_content(std::move(outgoing_body), "application/json");
            } else {
                // Candidate-loop validation guarantees this is unreachable,
                // but fail closed if future code violates that invariant.
                UsageTracker::UsageInfo zero;
                zero.model = model;
                enqueue_log(used->account.id, ar.route.local_key_id,
                                     zero, false, 502, fwd.duration_ms,
                                     used->key_slot_id, -1, -1, -1.0, -1, -1,
                                     attempts_made, attempts);
                res.status = 502;
                res.set_content(harness_codec.serialize_error_body(
                    json{{"message", "Invalid converted upstream response"},
                         {"type", "upstream_error"}}).dump(),
                    "application/json");
            }
        } else {
            // Non-2xx / upstream failure: record the failed attempt (zero
            // tokens, truthful status — 504 on timeout, else 502/upstream).
            UsageTracker::UsageInfo zu;
            zu.model = model;
            enqueue_log(used->account.id, ar.route.local_key_id,
                                 zu, false, fwd.status_code, fwd.duration_ms, used->key_slot_id,
                                 -1, -1, -1.0, -1, -1, attempts_made, attempts);
            json normalized;
            if (fwd.is_timeout) {
                normalized = timeout_error_body(fwd.timeout_secs);
            } else {
                try {
                    normalized = upstream_codec->parse_error_body(
                        json::parse(fwd.body));
                } catch (...) {
                    normalized = json{{"message", fwd.error.empty()
                                                      ? "upstream error"
                                                      : fwd.error}};
                }
            }
            res.status = (fwd.status_code >= 400) ? fwd.status_code : 502;
            if (fwd.is_timeout) res.set_header("Connection", "close");
            res.set_content(harness_codec.serialize_error_body(normalized).dump(),
                            "application/json");
        }
    }

    if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
        affinity_.bind(scope, session_id, used->key_slot_id);
        if (harness == ir::ApiFormat::OpenAIResponses)
            affinity_.bind(scope, response_id_from_body(fwd.body), used->key_slot_id);
    }
}

// ── handle_streaming ──────────────────────────────────────────────────

void ProxyServer::handle_streaming(
    const std::vector<UpstreamCandidate> &cands, size_t start,
    const std::string &session_id, int local_key_id, ir::ApiFormat harness,
    const std::string &resolved_model, const httplib::Request &req,
    httplib::Response &res, std::chrono::steady_clock::time_point t0) {
    const FormatCodec &harness_codec = codecs_.get(harness);
    ir::ChatRequest parsed_request;
    std::string parse_error;
    json request_json;
    try {
        request_json = json::parse(req.body);
    } catch (...) {
        res.status = 400;
        res.set_content(harness_codec.serialize_error_body(
                            json{{"message", "invalid JSON body"}, {"type", "parse_error"}}).dump(),
                        "application/json");
        return;
    }
    if (!harness_codec.parse_request(request_json, parsed_request, parse_error)) {
        res.status = 400;
        res.set_content(harness_codec.serialize_error_body(
                            json{{"message", parse_error}, {"type", "parse_error"}}).dump(),
                        "application/json");
        return;
    }

    const std::string content_type = req.has_header("Content-Type")
        ? req.get_header_value("Content-Type") : "application/json";
    const std::string scope = affinity_scope(local_key_id, harness);
    const auto order = candidate_order(
        cands, start, routing_rr_.fetch_add(1, std::memory_order_relaxed));
    const auto base_timeouts = timeout_config_cached(harness);
    const int budget_seconds = base_timeouts.streaming_first_byte_timeout > 0
        ? base_timeouts.streaming_first_byte_timeout : 60;
    const auto deadline = t0 + std::chrono::seconds(budget_seconds);
    res.set_chunked_content_provider(
        "text/event-stream",
        [this, cands, order, session_id, scope, local_key_id, harness, resolved_model,
         base_timeouts, deadline, budget_seconds,
         request_body = req.body, content_type, parsed_request, t0, &res,
         client_sock = req.client_socket](size_t, httplib::DataSink &sink) -> bool {
            const FormatCodec &out_codec = codecs_.get(harness);
            std::uint64_t inflight_id = 0;
            auto inflight_guard = make_scope_exit(
                [this, &inflight_id] { request_finished(inflight_id); });
            const UpstreamCandidate *used = nullptr;
            UpstreamClient::ForwardResult final_result;
            bool committed = false;
            bool last_timeout = false;
            int last_status = 429;
            int last_account_id = 0;
            int last_slot_id = 0;
            int last_duration_ms = 0;
            int attempts_made = 0;
            std::vector<Database::AttemptInfo> attempts;
            std::string last_body;
            json last_stream_error;
            bool first_semantic = true;
            std::chrono::steady_clock::time_point first_semantic_at;
            std::chrono::steady_clock::time_point last_semantic_at;
            int upstream_semantic_ttft = -1;
            std::string emitted_response_id;
            bool client_write_failed = false;
            bool terminal_error_forwarded = false;

            auto write_to_sink = [&](const std::string &data) -> bool {
                if (data.empty()) return true;
                bool ok = sink.write(data.data(), data.size());
                if (ok) committed = true;
                else client_write_failed = true;
                return ok;
            };
            auto emit_error = [&](const json &normalized) {
                auto emitter = out_codec.make_stream_emitter();
                ir::StreamEvent event;
                event.type = ir::StreamEventType::ErrorEvent;
                event.extra["error"] = normalized;
                emitter->emit(event, write_to_sink);
                emitter->finish(write_to_sink);
            };

            for (size_t attempt = 0; attempt < order.size(); ++attempt) {
                const auto &candidate = cands[order[attempt]];
                terminal_error_forwarded = false;
                if (!gate_.try_acquire_eligible(
                        candidate.key_slot_id,
                        candidate.account.max_concurrency)) continue;
                GateLease gate_lease(gate_, candidate.key_slot_id);

                if (inflight_id == 0) {
                    inflight_id = request_started(candidate.upstream_model, true);
                }

                const auto upstream = ir::parse_api_format(candidate.account.api_format);
                const bool passthrough = harness == upstream;
                const bool filter_thinking = passthrough && upstream == ir::ApiFormat::OpenAI;
                auto attempt_timeouts = base_timeouts;
                if (!clamp_to_remaining_budget(attempt_timeouts, deadline, true)) {
                    last_timeout = true;
                    last_status = 504;
                    final_result.status_code = 504;
                    final_result.is_timeout = true;
                    final_result.timeout_secs = budget_seconds;
                    break;
                }
                ++attempts_made;
                auto target = resolve_upstream_target(
                    candidate.account.api_format, candidate.account.base_url,
                    candidate.account.endpoint_path, candidate.account.auth_header,
                    attempt_timeouts);
                std::string body;
                if (passthrough) {
                    body = request_body;
                    apply_body_model(body, candidate.upstream_model);
                } else {
                    auto converted = parsed_request;
                    converted.model = candidate.upstream_model;
                    body = codecs_.get(upstream).serialize_request(converted).dump();
                }

                std::unique_ptr<ir::StreamParser> parser;
                std::unique_ptr<ir::StreamEmitter> emitter;
                // Parse a second, metrics-only IR stream for passthrough too;
                // raw SSE chunks/heartbeats are not TTFT.
                auto metrics_parser = codecs_.get(upstream).make_stream_parser();
                ThinkStreamFilter think_filter;
                bool attempt_first_semantic = true;
                auto attempt_semantic_seen =
                    std::make_shared<std::atomic<bool>>(false);
                auto attempt_semantic_progress =
                    std::make_shared<std::atomic<std::uint64_t>>(0);
                // Set once a protocol-complete terminal event has been
                // processed.  Its absence at a clean EOF classifies the
                // attempt as truncated rather than a full 2xx success.
                auto attempt_terminal_seen =
                    std::make_shared<std::atomic<bool>>(false);
                std::chrono::steady_clock::time_point attempt_first_semantic_at;
                std::chrono::steady_clock::time_point attempt_last_semantic_at;
                bool attempt_has_stream_error = false;
                json attempt_stream_error;
                int attempt_stream_error_status = 502;
                if (!passthrough) {
                    parser = codecs_.get(upstream).make_stream_parser();
                    emitter = out_codec.make_stream_emitter();
                }
                auto record_attempt_semantic =
                    [&](std::chrono::steady_clock::time_point now) {
                        attempt_semantic_progress->fetch_add(
                            1, std::memory_order_release);
                        if (attempt_first_semantic) {
                            attempt_first_semantic = false;
                            attempt_first_semantic_at = now;
                            attempt_semantic_seen->store(
                                true, std::memory_order_release);
                        }
                        attempt_last_semantic_at = now;
                    };
                auto promote_attempt_metrics = [&] {
                    if (attempt_first_semantic) return;
                    if (first_semantic) {
                        first_semantic = false;
                        first_semantic_at = attempt_first_semantic_at;
                    }
                    last_semantic_at = attempt_last_semantic_at;
                };
                auto write_attempt = [&](const std::string &data) -> bool {
                    if (data.empty()) return true;
                    const bool written = write_to_sink(data);
                    if (written) promote_attempt_metrics();
                    return written;
                };
                auto on_event = [&](const ir::StreamEvent &event) -> bool {
                    return emitter->emit(event, write_attempt);
                };
                auto on_metrics_event = [&](const ir::StreamEvent &event) -> bool {
                    if (event.type == ir::StreamEventType::ErrorEvent) {
                        attempt_has_stream_error = true;
                        attempt_stream_error = event.extra.contains("error")
                            ? event.extra["error"]
                            : json{{"message", "upstream stream error"}};
                        attempt_stream_error_status =
                            stream_error_status(attempt_stream_error);
                        // Stop before the raw/converted error frame is written.
                        // If nothing semantic was committed, the next candidate
                        // can now be tried safely.
                        return false;
                    }
                    if (event.type == ir::StreamEventType::MessageStart &&
                        event.extra.contains("id") && event.extra["id"].is_string())
                        emitted_response_id = event.extra["id"].get<std::string>();
                    // A protocol-complete terminal event is the only thing that
                    // makes a clean EOF a full 2xx success.  The metrics parser
                    // runs for passthrough and converted streams alike, so this
                    // single set covers both; codec finish() synthesis must NOT
                    // set it (that would hide a truncation).
                    if (event.type == ir::StreamEventType::MessageFinish)
                        attempt_terminal_seen->store(true,
                                                     std::memory_order_release);
                    const bool semantic =
                        (event.type == ir::StreamEventType::ContentTextDelta && !event.text.empty()) ||
                        (event.type == ir::StreamEventType::ContentThinkingDelta && !event.text.empty()) ||
                        event.type == ir::StreamEventType::ToolCallStart ||
                        (event.type == ir::StreamEventType::ToolCallArgumentDelta && !event.arguments.empty());
                    if (semantic) {
                        record_attempt_semantic(
                            std::chrono::steady_clock::now());
                    }
                    return true;
                };
                bool metrics_enabled = true;
                SemanticFallbackObserver semantic_fallback;
                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    const std::uint64_t progress_before =
                        attempt_semantic_progress->load(
                            std::memory_order_acquire);
                    if (metrics_enabled) {
                        try {
                            const bool observed = metrics_parser->feed(
                                data, len, on_metrics_event);
                            if (!observed && !attempt_has_stream_error) {
                                // Observability is best-effort. A provider
                                // extension the metrics parser cannot consume
                                // must never stop or delay passthrough traffic.
                                metrics_enabled = false;
                            }
                        } catch (const std::exception &e) {
                            fprintf(stderr,
                                    "[Proxy] metrics stream parser disabled: %s\n",
                                    e.what());
                            metrics_enabled = false;
                        } catch (...) {
                            fprintf(stderr,
                                    "[Proxy] metrics stream parser disabled\n");
                            metrics_enabled = false;
                        }
                    }
                    if (semantic_fallback.feed(data, len) &&
                        attempt_semantic_progress->load(
                            std::memory_order_acquire) == progress_before) {
                        record_attempt_semantic(
                            std::chrono::steady_clock::now());
                    }

                    // An error in the first upstream frame can still fall back
                    // without committing headers. Once anything was sent, the
                    // same frame must be forwarded and the stream terminated.
                    if (attempt_has_stream_error && !committed) return false;

                    bool forwarded = true;
                    if (!passthrough) {
                        forwarded = parser->feed(data, len, on_event);
                    } else if (!filter_thinking) {
                        forwarded = write_attempt(std::string(data, len));
                    } else {
                        std::string filtered = think_filter.feed(data, len);
                        forwarded = filtered.empty() || write_attempt(filtered);
                    }
                    if (attempt_has_stream_error && committed)
                        terminal_error_forwarded = true;
                    return forwarded && !attempt_has_stream_error;
                };

                const auto attempt_started = std::chrono::steady_clock::now();
                target.opts.semantic_seen = attempt_semantic_seen;
                target.opts.semantic_progress = attempt_semantic_progress;
                // Strict mode wires the truncation detector; disabled leaves
                // opts.terminal_seen null so forward() keeps old behavior.
                if (strict_terminal_enabled())
                    target.opts.terminal_seen = attempt_terminal_seen;
                target.opts.downstream_socket = client_sock;
                auto result = upstream_.forward(
                    "POST", candidate.account.base_url, candidate.key, target.path, body,
                    passthrough ? content_type : "application/json", on_chunk,
                    target.opts);

                // Flush the observer before classifying a 2xx response. SSE
                // permits the final frame to end at EOF without a blank line.
                const std::uint64_t finish_progress_before =
                    attempt_semantic_progress->load(
                        std::memory_order_acquire);
                if (metrics_enabled) {
                    try {
                        metrics_parser->finish(on_metrics_event);
                    } catch (const std::exception &e) {
                        fprintf(stderr,
                                "[Proxy] metrics stream finish ignored: %s\n",
                                e.what());
                    } catch (...) {
                        fprintf(stderr,
                                "[Proxy] metrics stream finish ignored\n");
                    }
                }
                if (semantic_fallback.finish() &&
                    attempt_semantic_progress->load(
                        std::memory_order_acquire) == finish_progress_before) {
                    record_attempt_semantic(std::chrono::steady_clock::now());
                }
                if (attempt_has_stream_error && !result.client_disconnected) {
                    result.status_code = attempt_stream_error_status;
                    result.success = false;
                    result.is_timeout = false;
                    result.timeout_secs = 0;
                    result.error = stream_error_message(attempt_stream_error);
                }
                const bool downstream_before_flush =
                    result.client_disconnected || client_write_failed ||
                    client_socket_gone(client_sock);
                if (result.success && result.status_code >= 200 &&
                    result.status_code < 300) {
                    gate_.mark_success(candidate.key_slot_id);
                } else if (!downstream_before_flush &&
                           candidate_failure_retryable(result.status_code)) {
                    gate_.record_failure(
                        candidate.key_slot_id,
                        account_types::cooldown_class(candidate.account.account_type),
                        result.usage_limit, result.status_code);
                }
                // State is published before the slot becomes available. All
                // downstream writes and logging happen after this point.
                gate_lease.release();
                if (committed) promote_attempt_metrics();

                bool filter_tail_flushed = false;
                if (passthrough && filter_thinking && !client_write_failed &&
                    (committed ||
                     (result.success && !attempt_has_stream_error))) {
                    std::string tail = think_filter.finish();
                    if (!tail.empty()) {
                        const bool ends_lf = tail.back() == '\n';
                        const bool has_blank =
                            (tail.size() >= 2 &&
                             tail.compare(tail.size() - 2, 2, "\n\n") == 0) ||
                            (tail.size() >= 4 &&
                             tail.compare(tail.size() - 4, 4,
                                          "\r\n\r\n") == 0);
                        if (!has_blank) tail += ends_lf ? "\n" : "\n\n";
                        filter_tail_flushed = write_attempt(tail);
                        if (attempt_has_stream_error && filter_tail_flushed)
                            terminal_error_forwarded = true;
                    }
                }

                if (attempt_has_stream_error && committed && !passthrough) {
                    // The conversion parser may still hold the EOF-terminated
                    // frame; flush it through the existing stateful emitter.
                    parser->finish(on_event);
                    emitter->finish(write_attempt);
                    terminal_error_forwarded = true;
                } else if (attempt_has_stream_error && committed && passthrough &&
                           !filter_tail_flushed &&
                           (result.body.size() < 2 ||
                            result.body.compare(result.body.size() - 2, 2,
                                                "\n\n") != 0) &&
                           (result.body.size() < 4 ||
                            result.body.compare(result.body.size() - 4, 4,
                                                "\r\n\r\n") != 0)) {
                    // Complete an EOF-terminated passthrough SSE frame.
                    if (write_attempt("\n\n")) terminal_error_forwarded = true;
                }
                if (result.success && result.status_code >= 200 && result.status_code < 300) {
                    if (!passthrough) {
                        parser->finish(on_event);
                        emitter->finish(write_attempt);
                    }
                    if (!attempt_first_semantic) {
                        upstream_semantic_ttft = static_cast<int>(
                            std::chrono::duration_cast<std::chrono::milliseconds>(
                                attempt_first_semantic_at - attempt_started).count());
                    }
                    attempts.push_back(attempt_info(candidate, result,
                                                    upstream_semantic_ttft));
                    final_result = std::move(result);
                    used = &candidate;
                    break;
                }

                const bool downstream_gone = result.client_disconnected ||
                                             client_write_failed ||
                                             client_socket_gone(client_sock);
                last_timeout = result.is_timeout;
                last_status = result.status_code;
                last_account_id = candidate.account.id;
                last_slot_id = candidate.key_slot_id;
                last_duration_ms = result.duration_ms;
                last_body = result.body;
                last_stream_error = attempt_has_stream_error
                    ? attempt_stream_error : json();
                attempts.push_back(attempt_info(candidate, result,
                    attempt_first_semantic ? -1 : static_cast<int>(
                        std::chrono::duration_cast<std::chrono::milliseconds>(
                            attempt_first_semantic_at - attempt_started).count())));
                final_result = std::move(result);

                // A parser can receive bytes without emitting a harness event;
                // only a successful sink.write commits this client response.
                if (downstream_gone || committed ||
                    !candidate_failure_retryable(last_status)) break;
            }

            inflight_guard.run_now();
            if (used && client_write_failed) {
                UsageTracker::UsageInfo zero;
                zero.model = resolved_model;
                enqueue_log(used->account.id, local_key_id, zero, true,
                                     499, final_result.duration_ms,
                                     used->key_slot_id, -1, -1, -1.0, -1,
                                     final_result.duration_ms,
                                     static_cast<int>(attempts.size()), attempts);
                return false;
            }
            if (used) {
                auto usage = UsageTracker::parse_stream_usage(
                    ir::to_string(ir::parse_api_format(used->account.api_format)), final_result.body);
                if (!usage) {
                    UsageTracker::UsageInfo zero;
                    zero.model = resolved_model;
                    usage = zero;
                }
                usage->model = resolved_model;
                const int proxy_ttft = first_semantic ? -1 : static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_semantic_at - t0).count());
                const int generation_ms = first_semantic ? -1 : static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        last_semantic_at - first_semantic_at).count());
                // The interval starts when the first output arrives, so it
                // contains completion_tokens-1 generation intervals.
                const double output_tps = generation_ms > 0 && usage->completion_tokens > 1
                    ? (usage->completion_tokens - 1) * 1000.0 / generation_ms : -1.0;
                enqueue_log(used->account.id, local_key_id, *usage, true,
                                     final_result.status_code, final_result.duration_ms, used->key_slot_id,
                                     proxy_ttft, generation_ms, output_tps,
                                     upstream_semantic_ttft, final_result.duration_ms,
                                     attempts_made, attempts);
                affinity_.bind(scope, session_id, used->key_slot_id);
                if (harness == ir::ApiFormat::OpenAIResponses) {
                    affinity_.bind(scope, emitted_response_id.empty()
                                       ? response_id_from_body(final_result.body)
                                       : emitted_response_id,
                                   used->key_slot_id);
                }
                sink.done();
                return true;
            }

            if (client_write_failed || final_result.client_disconnected ||
                client_socket_gone(client_sock)) {
                if (last_account_id) {
                    UsageTracker::UsageInfo zero;
                    zero.model = resolved_model;
                    enqueue_log(last_account_id, local_key_id, zero, true,
                                         499, last_duration_ms, last_slot_id,
                                         -1, -1, -1.0, -1,
                                         final_result.duration_ms,
                                         static_cast<int>(attempts.size()), attempts);
                }
                return false;
            }
            const int final_status = last_timeout ? 504 : last_status;
            res.status = final_status;
            json normalized = last_timeout
                ? timeout_error_body(final_result.timeout_secs)
                : (!last_stream_error.is_null()
                    ? last_stream_error
                    : json{{"message", last_body.empty()
                        ? "All upstream accounts are busy, cooling down, or failed" : last_body},
                           {"type", final_status == 429 ? "rate_limit_error" : "upstream_error"},
                           {"code", final_status}});
            if (last_account_id) {
                UsageTracker::UsageInfo zero;
                zero.model = resolved_model;
                enqueue_log(last_account_id, local_key_id, zero, true,
                                     final_status, last_duration_ms, last_slot_id,
                                     -1, -1, -1.0, -1,
                                     final_result.duration_ms, attempts_made, attempts);
            } else {
                UsageTracker::UsageInfo zero;
                zero.model = resolved_model;
                enqueue_log(cands.front().account.id, local_key_id,
                                     zero, true, final_status, 0, 0,
                                     -1, -1, -1.0, -1, -1, 0, attempts);
            }
            if (!committed || !terminal_error_forwarded)
                emit_error(normalized);
            sink.done();
            return true;
        },
        nullptr);
    res.set_deferred_chunked_headers();
}


// ── handle_list_models ─────────────────────────────────────────────────

void ProxyServer::handle_list_models(const httplib::Request &req,
                                      httplib::Response &res) {
    add_cors_headers(res);

    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    // Claude Code (cc) validates its configured model name against /v1/models
    // and refuses to start if it can't find it.  Like cc-switch, answer
    // Anthropic clients with an empty catalog so any model — including the
    // `[1m]`/`[1M]`-suffixed names the proxy strips before forwarding — is
    // accepted by the client.  (The `[1m]` marker is a cc-only local
    // capability suffix; other clients must only ever see real model names.)
    if (req.has_header("anthropic-version")) {
        res.status = 200;
        res.set_content("{\"models\":[]}", "application/json");
        return;
    }

    // Aggregate accounts expose their entry patterns as the model catalog —
    // real names only, no `[1m]`/`[1M]` aliases (those are internal to cc).
    if (ar.route.is_aggregate) {
        auto patterns = db_.get_aggregate_model_patterns(ar.route.account_id);
        json out = json::array();
        for (const auto &p : patterns) {
            json m = {{"id", p}, {"object", "model"}, {"created", 1},
                      {"owned_by", "token-board"}};
            out.push_back(m);
        }
        res.status = 200;
        res.set_content(json{{"object", "list"}, {"data", std::move(out)}}.dump(),
                        "application/json");
        return;
    }

    // Plain accounts use the same consistent multi-key snapshot, per-key
    // concurrency gate, authentication scheme, cancellation and failover as
    // chat/embeddings. A revoked first key must not make /v1/models fail while
    // ordinary requests correctly spill to a healthy sibling.
    std::string catalog_model;
    auto cands = resolve_candidates_cached(ar.route, catalog_model);
    if (cands.empty()) {
        res.status = 503;
        res.set_content(json_error("No upstream key is configured", 503),
                        "application/json");
        return;
    }

    const auto catalog_timeouts = timeout_config_cached(ir::ApiFormat::OpenAI);
    const int budget_seconds = catalog_timeouts.non_streaming_timeout > 0
        ? catalog_timeouts.non_streaming_timeout : 600;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(budget_seconds);
    UpstreamClient::ForwardResult fwd;
    const UpstreamCandidate *used = nullptr;
    bool attempted = false;
    std::uint64_t inflight_id = 0;
    auto inflight_guard = make_scope_exit(
        [this, &inflight_id] { request_finished(inflight_id); });

    for (const auto &candidate : cands) {
        if (!gate_.try_acquire_eligible(candidate.key_slot_id,
                                        candidate.account.max_concurrency))
            continue;
        GateLease gate_lease(gate_, candidate.key_slot_id);
        if (inflight_id == 0)
            inflight_id = request_started("/v1/models", false);

        auto attempt_timeouts = catalog_timeouts;
        if (!clamp_to_remaining_budget(attempt_timeouts, deadline, false)) {
            fwd.status_code = 504;
            fwd.is_timeout = true;
            fwd.timeout_secs = budget_seconds;
            fwd.error = "request retry budget exhausted";
            break;
        }

        ForwardOptions mopts;
        mopts.non_streaming_timeout = attempt_timeouts.non_streaming_timeout;
        mopts.non_streaming_total_timeout = attempt_timeouts.non_streaming_timeout;
        mopts.downstream_socket = req.client_socket;
        mopts.auth_scheme = candidate.account.auth_header;
        if (mopts.auth_scheme.empty() || mopts.auth_scheme == "auto") {
            mopts.auth_scheme = candidate.account.api_format == "anthropic"
                ? "x-api-key" : "bearer";
        }
        attempted = true;
        fwd = upstream_.forward("GET", candidate.account.base_url,
                                candidate.key, "/models", "",
                                "application/json", nullptr, mopts);

        const bool downstream_gone = fwd.client_disconnected ||
                                     client_socket_gone(req.client_socket);
        if (fwd.success) {
            gate_.mark_success(candidate.key_slot_id);
            used = &candidate;
            break;
        }
        if (!downstream_gone && candidate_failure_retryable(fwd.status_code))
            gate_.record_failure(candidate.key_slot_id,
                                 account_types::cooldown_class(candidate.account.account_type),
                                 fwd.usage_limit, fwd.status_code);
        gate_lease.release();
        if (downstream_gone || !candidate_failure_retryable(fwd.status_code)) {
            used = &candidate;
            break;
        }
    }
    inflight_guard.run_now();

    if (!used && !attempted && !fwd.is_timeout) {
        res.status = 429;
        res.set_content(json_error("All upstream keys are busy or cooling down",
                                   429),
                        "application/json");
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    if (fwd.success) {
        // Real upstream model catalog, passed through unmodified — no
        // `[1m]`/`[1M]` aliases (those are internal to cc's Anthropic flow).
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        res.status = fwd.status_code;
        if (fwd.is_timeout) {
            // Explicit, retryable timeout error instead of a generic error.
            res.set_header("Connection", "close");
            res.set_content(json{{"error",
                                  timeout_error_body(fwd.timeout_secs)}}.dump(),
                            "application/json");
        } else {
            res.set_content(json_error("Upstream error: " + fwd.error,
                                       fwd.status_code),
                            "application/json");
        }
    }
}

// ── handle_embeddings ────────────────────────────────────────────────────

void ProxyServer::handle_embeddings(const httplib::Request &req,
                                    httplib::Response &res) {
    add_cors_headers(res);

    auto t0 = std::chrono::steady_clock::now();

    // 1. Auth + route lookup
    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    // 2. Resolve effective upstream targets (strip [1m] marker; aggregate
    //    routing yields multiple candidates in priority order).
    std::string body = req.body;
    std::string req_model = extract_model(body);
    auto cands = resolve_candidates_cached(ar.route, req_model);
    if (cands.empty()) {
        res.status = 400;
        res.set_content(json_error("Model '" + req_model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }

    // 3. Determine content type
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    // Embeddings are always non-streaming: try candidates in order, falling
    // back on 429/5xx (nothing has been sent to the client yet).
    std::uint64_t inflight_id = 0;
    auto inflight_guard = make_scope_exit(
        [this, &inflight_id] { request_finished(inflight_id); });
    int concurrent_count = 0;
    UpstreamClient::ForwardResult fwd;
    const UpstreamCandidate *used = nullptr;
    const UpstreamCandidate *last_attempted = nullptr;
    std::vector<Database::AttemptInfo> attempts;
    const auto embedding_timeouts = timeout_config_cached(ir::ApiFormat::OpenAI);
    const int embedding_budget_seconds = embedding_timeouts.non_streaming_timeout > 0
        ? embedding_timeouts.non_streaming_timeout : 600;
    const auto embedding_deadline = t0 + std::chrono::seconds(embedding_budget_seconds);

    for (size_t i = 0; i < cands.size(); ++i) {
        const UpstreamCandidate &c = cands[i];
        if (!gate_.try_acquire_eligible(c.key_slot_id,
                                        c.account.max_concurrency)) continue;
        GateLease gate_lease(gate_, c.key_slot_id);

        if (inflight_id == 0) {
            inflight_id = request_started(c.upstream_model, false);
            concurrent_count = in_flight_count();
        }

        fprintf(stderr, "[Proxy] embedding request from key_id=%d to account=%d "
                        "model=%s (concurrent=%d, inflight_id=%llu)\n",
                ar.route.local_key_id, c.account.id,
                c.upstream_model.c_str(), concurrent_count,
                static_cast<unsigned long long>(inflight_id));

        std::string eb = req.body;
        apply_body_model(eb, c.upstream_model);
        auto attempt_timeouts = embedding_timeouts;
        if (!clamp_to_remaining_budget(attempt_timeouts, embedding_deadline, false)) {
            fwd.status_code = 504;
            fwd.is_timeout = true;
            fwd.timeout_secs = embedding_budget_seconds;
            break;
        }
        ForwardOptions eopts;
        eopts.non_streaming_timeout = attempt_timeouts.non_streaming_timeout;
        eopts.non_streaming_total_timeout = attempt_timeouts.non_streaming_timeout;
        eopts.downstream_socket = req.client_socket;
        fwd = upstream_.forward(
            "POST", c.account.base_url, c.key,
            "/embeddings", eb, content_type, nullptr, eopts);
        last_attempted = &c;
        attempts.push_back(attempt_info(c, fwd));

        const bool downstream_gone = fwd.client_disconnected ||
                                     client_socket_gone(req.client_socket);
        if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300)
            gate_.mark_success(c.key_slot_id);
        else if (!downstream_gone &&
                 candidate_failure_retryable(fwd.status_code))
            gate_.record_failure(c.key_slot_id,
                                 account_types::cooldown_class(c.account.account_type),
                                 fwd.usage_limit, fwd.status_code);
        gate_lease.release();

        bool retryable = !downstream_gone &&
                         candidate_failure_retryable(fwd.status_code) &&
                         i + 1 < cands.size();
        if (retryable) {
            fprintf(stderr, "[Proxy] upstream %d (%s) failed (%d), trying "
                            "next candidate\n",
                    c.account.id, c.account.name.c_str(), fwd.status_code);
            continue;
        }
        used = &c;
        break;
    }

    if (!used) {
        inflight_guard.run_now();
        UsageTracker::UsageInfo zero;
        zero.model = req_model;
        const int final_status = fwd.is_timeout ? 504
            : (!attempts.empty() && fwd.status_code >= 400
                ? fwd.status_code : 429);
        enqueue_log(last_attempted ? last_attempted->account.id
                                            : ar.route.account_id,
                             ar.route.local_key_id, zero,
                             false, final_status,
                             static_cast<int>(std::chrono::duration_cast<
                                 std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count()),
                             last_attempted ? last_attempted->key_slot_id : 0,
                             -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
        if (fwd.is_timeout) {
            // Every candidate timed out — a timeout error, not a busy signal.
            res.status = 504;
            res.set_header("Connection", "close");
            res.set_content(json{{"error",
                                  timeout_error_body(fwd.timeout_secs)}}.dump(),
                            "application/json");
        } else if (attempts.empty()) {
            res.status = 429;
            res.set_content(json_error("All upstream accounts are busy, cooling "
                                       "down, or failed", 429),
                            "application/json");
        } else {
            res.status = final_status;
            res.set_content(json_error("Upstream error: " +
                                           (fwd.error.empty() ? fwd.body
                                                              : fwd.error),
                                       final_status),
                            "application/json");
        }
        return;
    }

    // ── Check if client disconnected while waiting for upstream ──
    if (fwd.client_disconnected || client_socket_gone(req.client_socket)) {
            fprintf(stderr, "[Proxy] Client gone (embeddings), drop response "
                    "(inflight=%llu, model=%s)\n",
                    static_cast<unsigned long long>(inflight_id),
                    req_model.c_str());
            UsageTracker::UsageInfo zero;
            zero.model = req_model;
            inflight_guard.run_now();
            enqueue_log(used->account.id, ar.route.local_key_id,
                                 zero, false, 499, fwd.duration_ms,
                                 used->key_slot_id, -1, -1, -1.0, -1, -1,
                                 static_cast<int>(attempts.size()), attempts);
            return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));
    inflight_guard.run_now();

    // Parse usage
    auto usage = UsageTracker::parse_usage(fwd.body);
    if (usage.has_value()) {
        enqueue_log(used->account.id,
                             ar.route.local_key_id,
                             *usage, false, fwd.status_code,
                             fwd.duration_ms, used->key_slot_id,
                             -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
    } else {
        fprintf(stderr, "[Proxy] Warning: could not parse usage "
                        "from embedding response, model=%s\n",
                        req_model.c_str());
        UsageTracker::UsageInfo zero;
        zero.model = req_model;
        enqueue_log(used->account.id, ar.route.local_key_id,
                             zero, false, fwd.status_code, fwd.duration_ms,
                             used->key_slot_id, -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
    }

    if (fwd.success) {
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        res.status = fwd.status_code;
        if (fwd.is_timeout) {
            // Explicit, retryable timeout error instead of a generic error.
            res.set_header("Connection", "close");
            res.set_content(json{{"error",
                                  timeout_error_body(fwd.timeout_secs)}}.dump(),
                            "application/json");
        } else {
            res.set_content(
                json_error("Upstream error: " + fwd.error, fwd.status_code),
                "application/json");
        }
    }
}

// ── setup_routes ─────────────────────────────────────────────────────────

void ProxyServer::setup_routes(httplib::Server &server) {
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
    server.Get("/health", [this](const httplib::Request &, httplib::Response &res) {
        add_cors_headers(res);
        json j;
        j["status"] = "ok";
        j["service"] = "token-board-proxy";
        j["concurrency"] = in_flight_count();
        res.set_content(j.dump(), "application/json");
    });
}
