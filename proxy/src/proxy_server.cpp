#include "proxy_server.h"
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
#include <cstdio>
#include <poll.h>
#include <sys/socket.h>
#include <thread>

using json = nlohmann::json;

// ── Helpers ──────────────────────────────────────────────────────────────

/// Check whether a request body has "stream": true (best-effort substring
/// match — avoids full JSON parse just to check streaming mode).
static bool is_streaming_request(const std::string &body) {
    return body.find("\"stream\"") != std::string::npos &&
           body.find("true") != std::string::npos;
}

/// Best-effort extract the model name from a JSON request body.
static std::string extract_model(const std::string &body) {
    // Look for "model": "..." pattern
    auto pos = body.find("\"model\"");
    if (pos == std::string::npos) return "unknown";
    auto colon = body.find(':', pos);
    if (colon == std::string::npos) return "unknown";
    auto q1 = body.find('"', colon + 1);
    if (q1 == std::string::npos) return "unknown";
    auto q2 = body.find('"', q1 + 1);
    if (q2 == std::string::npos) return "unknown";
    return body.substr(q1 + 1, q2 - q1 - 1);
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

/// Build a JSON error response.
static std::string json_error(const std::string &msg, int code) {
    json j;
    j["error"] = {{"message", msg}, {"type", "auth_error"}, {"code", code}};
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

/// SSE error frame for the passthrough streaming path (no codec available).
/// Emits a terminal error event in the client's wire format instead of
/// silently dropping the connection, so the client knows the upstream never
/// replied and can prompt the user to retry.
static std::string timeout_sse_frame(ir::ApiFormat fmt, int timeout_secs = 0) {
    json err = timeout_error_body(timeout_secs);
    switch (fmt) {
        case ir::ApiFormat::Anthropic:
            return "event: error\ndata: " +
                   json{{"type", "error"},
                        {"error", json{{"type", "timeout_error"},
                                       {"message", err["message"]}}}}
                       .dump() +
                   "\n\n";
        case ir::ApiFormat::OpenAIResponses:
            return "data: " +
                   json{{"type", "response.failed"},
                        {"response", json{{"id", ""},
                                          {"object", "response"},
                                          {"status", "failed"},
                                          {"error", err}}}}
                       .dump() +
                   "\n\n";
        default:  // OpenAI chat completions
            return "data: " + json{{"error", err}}.dump() + "\n\n"
                   "data: [DONE]\n\n";
    }
}

/// Shared state for client-disconnect monitoring: the upstream socket fd is
/// captured by `UpstreamClient::forward` via `on_socket`; the monitor thread
/// shutdown()s it to unblock an in-flight upstream read once the client
/// disconnects, so the pool thread is freed promptly instead of waiting out
/// the full 100s read timeout.
struct AbortGuard {
    std::atomic<bool> running{false};
    std::atomic<int> upstream_fd{-1};
};

/// Spawn a monitor thread that polls the client socket every 250ms.  When the
/// client disconnects (POLLHUP/POLLERR/POLLRDHUP), shutdown() the upstream fd
/// so the blocked upstream read returns immediately.  Returns a joinable
/// thread; the caller must set running=false and join() after the upstream
/// forward returns.
static std::thread spawn_client_monitor(int client_sock, AbortGuard &g) {
    g.running.store(true, std::memory_order_release);
    return std::thread([&g, client_sock] {
        while (g.running.load(std::memory_order_acquire)) {
            struct pollfd pfd;
            pfd.fd = client_sock;
            // POLLRDHUP must be requested in events: for TCP a peer's FIN only
            // surfaces as POLLIN (EOF) + POLLRDHUP, never POLLHUP — without it
            // client disconnects would never be detected.
            pfd.events = POLLIN | POLLRDHUP;
            pfd.revents = 0;
            int r = ::poll(&pfd, 1, 250);   // 250ms interval
            if (r > 0 && (pfd.revents & (POLLHUP | POLLERR | POLLRDHUP))) {
                int fd = g.upstream_fd.load(std::memory_order_relaxed);
                if (fd >= 0 && g.running.load(std::memory_order_acquire))
                    ::shutdown(fd, SHUT_RDWR);   // unblock the upstream read
                return;
            }
        }
    });
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
static void push_account_candidates(Database &db,
                                    std::vector<UpstreamCandidate> &cands,
                                    const Database::AccountInfo &acct,
                                    const std::string &model) {
    auto keys = db.get_upstream_keys(acct.id);
    if (keys.empty()) {
        // Legacy single-key fallback: use the account's upstream_key as a
        // single candidate.  Concurrency slot = -account_id (negative, unique
        // per account — never collides with real upstream_keys.id which is
        // always positive), so legacy accounts keep independent budgets.
        UpstreamCandidate c;
        c.account = acct;
        c.key = acct.upstream_key;
        c.key_slot_id = -acct.id;
        c.upstream_model = model;
        cands.push_back(std::move(c));
    } else {
        // One candidate per key slot, same account config, ordered by
        // (position, id) so overflow follows a fixed fill order.
        for (auto &k : keys) {
            UpstreamCandidate c;
            c.account = acct;          // keys share the account config
            c.key = std::move(k.key_value);
            c.key_slot_id = k.id;
            c.upstream_model = model;
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
resolve_candidates(Database &db, const Router::RouteResult &route,
                   std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    std::vector<UpstreamCandidate> cands;
    if (route.is_aggregate) {
        auto entries = db.resolve_aggregate(route.account_id, model);
        for (const auto &e : entries) {
            auto acct = db.get_account(e.upstream_account_id);
            if (!acct.has_value() || acct->deleted) {
                fprintf(stderr, "[Proxy] aggregate account %d: upstream account %d missing/deleted\n",
                        route.account_id, e.upstream_account_id);
                continue;
            }
            fprintf(stderr, "[Proxy] aggregate %d: %s → account %d (%s) model %s\n",
                    route.account_id, model.c_str(), acct->id, acct->name.c_str(),
                    e.upstream_model.c_str());
            push_account_candidates(db, cands, *acct, e.upstream_model);
        }
    } else {
        auto acct = db.get_account(route.account_id);
        if (!acct.has_value() || acct->deleted) return cands;
        push_account_candidates(db, cands, *acct, model);
    }
    return cands;
}

/// Pick a candidate starting at `start` and advancing in cyclic order
/// (start, start+1, …, n-1, 0, …, start-1).  Concurrency and plan cooldown
/// are both tracked per key slot.  Returns the picked index via `picked`.
static bool pick_candidate(AccountGate &gate,
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
                   "Authorization, Content-Type");
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
    t.opts.streaming_idle_timeout = tc.streaming_idle_timeout;
    t.opts.non_streaming_timeout = tc.non_streaming_timeout;
    return t;
}

/// Per-format timeout config for a client request, mirroring cc-switch's
/// per-app-type timeouts keyed here by the client's wire format.
static Database::TimeoutConfig timeout_config_for(Database &db,
                                                  ir::ApiFormat harness) {
    switch (harness) {
        case ir::ApiFormat::Anthropic:
            return db.get_timeout_config("anthropic");
        case ir::ApiFormat::OpenAIResponses:
            return db.get_timeout_config("openai_responses");
        default:  // OpenAI chat
            return db.get_timeout_config("openai");
    }
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
static bool client_disconnected(const httplib::Request &req, int inflight_id,
                                const std::string &model) {
    if (req.client_socket == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = req.client_socket;
    // Same POLLRDHUP caveat as spawn_client_monitor: a peer's FIN surfaces
    // as POLLIN|POLLRDHUP, never POLLHUP, and POLLRDHUP must be requested.
    pfd.events = POLLIN | POLLRDHUP;
    pfd.revents = 0;
    if (poll(&pfd, 1, 0) > 0 &&
        (pfd.revents & (POLLHUP | POLLERR | POLLRDHUP))) {
        fprintf(stderr, "[Proxy] Client gone, drop response "
                        "(inflight=%d, model=%s)\n", inflight_id, model.c_str());
        return true;
    }
    return false;
}

/// True if the client socket is already closed.  Used in the streaming paths to
/// distinguish a genuine upstream timeout from a client disconnect: the monitor
/// thread shutdown()s the upstream socket when the client goes away, which the
/// upstream client surfaces as a read error (is_timeout).
static bool client_socket_gone(int sock) {
    if (sock == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = sock;
    pfd.events = POLLIN | POLLRDHUP;
    pfd.revents = 0;
    return poll(&pfd, 1, 0) > 0 &&
           (pfd.revents & (POLLHUP | POLLERR | POLLRDHUP));
}

/// Synchronous non-streaming forward with client-disconnect monitoring.
static UpstreamClient::ForwardResult
forward_once(UpstreamClient &upstream, const std::string &body,
             const std::string &content_type, const UpstreamCandidate &c,
             const UpstreamTarget &target, int client_sock) {
    AbortGuard abort;
    auto monitor = spawn_client_monitor(client_sock, abort);
    auto fwd = upstream.forward(
        "POST", c.account.base_url, c.key,
        target.path, body, content_type, nullptr, target.opts,
        [&](int fd) {
            abort.upstream_fd.store(fd, std::memory_order_relaxed);
        });
    abort.running.store(false, std::memory_order_release);
    if (monitor.joinable()) monitor.join();
    return fwd;
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
    auto cands = resolve_candidates(db_, ar.route, model);
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
        size_t start = static_cast<size_t>(
            affinity_.preferred_index(session_id, cands.size()));
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

    int inflight_id = -1;
    int concurrent_count = 0;
    UpstreamClient::ForwardResult fwd;
    const UpstreamCandidate *used = nullptr;
    bool think_filter = false;
    ir::ApiFormat used_upstream_fmt = harness;
    const FormatCodec *upstream_codec = nullptr;

    // Session-affinity spillover: start at the session's preferred candidate
    // and wrap around in fixed order (P, P+1, …, n-1, 0, …, P-1).
    std::string session_id = extract_session_id(req, req_json);
    size_t start = static_cast<size_t>(
        affinity_.preferred_index(session_id, cands.size()));

    for (size_t i = 0; i < cands.size(); ++i) {
        size_t ci = (start + i) % cands.size();
        const UpstreamCandidate &c = cands[ci];
        if (gate_.in_cooldown(c.key_slot_id)) continue;
        if (!gate_.acquire(c.key_slot_id, c.account.max_concurrency)) continue;

        if (inflight_id < 0) {
            inflight_id = db_.request_start(ar.route.local_key_id, c.account.id,
                                            c.upstream_model, false);
            concurrent_count = db_.get_in_flight_count();
        }

        ir::ApiFormat upstream = ir::parse_api_format(c.account.api_format);
        auto target = resolve_upstream_target(
            c.account.api_format, c.account.base_url,
            c.account.endpoint_path, c.account.auth_header,
            timeout_config_for(db_, harness));

        fprintf(stderr, "[Proxy] %s %s request from key_id=%d to account=%d "
                        "model=%s (concurrent=%d, inflight_id=%d)\n",
                ir::to_string(harness).c_str(),
                (harness == upstream) ? "passthrough" : "convert",
                ar.route.local_key_id, c.account.id,
                c.upstream_model.c_str(), concurrent_count, inflight_id);

        if (harness == upstream) {
            std::string body = req.body;
            apply_body_model(body, c.upstream_model);
            fwd = forward_once(upstream_, body, content_type, c, target,
                               req.client_socket);
            think_filter = (upstream == ir::ApiFormat::OpenAI);
        } else {
            cReq.model = c.upstream_model;
            upstream_codec = &codecs_.get(upstream);
            std::string body = upstream_codec->serialize_request(cReq).dump();
            fwd = forward_once(upstream_, body, "application/json", c, target,
                               req.client_socket);
            think_filter = false;
        }
        used_upstream_fmt = upstream;

        // plan keys cool down for 5h when the upstream rate-limits them.
        if (fwd.status_code == 429 && c.account.account_type == "plan") {
            fprintf(stderr, "[Proxy] key slot %d (account %d %s) plan "
                            "rate-limited (429), cooling down 5h\n",
                    c.key_slot_id, c.account.id, c.account.name.c_str());
            gate_.mark_cooldown(c.key_slot_id);
        }

        // Fall back to the next candidate on 429/5xx (including upstream
        // timeouts — a stuck upstream is a provider failure, so switch), as
        // long as nothing was sent to the client yet (non-streaming: nothing
        // was).  Mirrors cc-switch, which classifies timeouts as retryable.
        bool retryable = (fwd.status_code == 429 || fwd.status_code >= 500) &&
                         i + 1 < cands.size();
        if (retryable) {
            fprintf(stderr, "[Proxy] upstream %d (%s) failed (%d), trying "
                            "next candidate\n",
                    c.account.id, c.account.name.c_str(), fwd.status_code);
            gate_.release(c.key_slot_id);
            continue;
        }
        used = &c;
        break;
    }

    if (used) {
        // Log the (session → key) binding once when established/changed.
        if (affinity_.binding_changed(session_id, used->key_slot_id))
            db_.log_session_key(session_id, used->account.id,
                                used->key_slot_id);
    }

    if (!used) {
        if (inflight_id >= 0) db_.request_end(inflight_id);
        if (fwd.is_timeout) {
            // Every candidate timed out (the last forward was a timeout) — a
            // timeout error, not a busy signal.
            res.status = 504;
            res.set_header("Connection", "close");
            res.set_content(json{{"error",
                                  timeout_error_body(fwd.timeout_secs)}}.dump(),
                            "application/json");
        } else {
            res.status = 429;
            res.set_content(json_error("All upstream accounts are busy, cooling "
                                       "down, or failed", 429),
                            "application/json");
        }
        return;
    }

    if (client_disconnected(req, inflight_id, model)) {
        // Record the aborted request truthfully (client closed before we
        // could send a response): status 499, zero tokens.
        UsageTracker::UsageInfo zu;
        zu.model = model;
        int dur = static_cast<int>(std::chrono::duration_cast<
            std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0)
                .count());
        tracker_.log_request(used->account.id, ar.route.local_key_id, zu,
                             false, 499, dur, used->key_slot_id);
        db_.request_end(inflight_id);
        gate_.release(used->key_slot_id);
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    // ── Non-streaming response handling (passthrough vs converted) ──
    if (used_upstream_fmt == harness) {
        auto usage = parse_usage_for_format(ir::to_string(used_upstream_fmt),
                                            fwd.body);
        if (usage.has_value()) {
            tracker_.log_request(used->account.id, ar.route.local_key_id,
                                 *usage, false, fwd.status_code,
                                 fwd.duration_ms, used->key_slot_id);
        } else {
            fprintf(stderr, "[Proxy] Warning: could not parse usage "
                            "from non-streaming response, model=%s\n",
                    model.c_str());
            // No usage to record — still log the attempt (zero tokens) so the
            // request_log is a complete, truthful record.
            UsageTracker::UsageInfo zu;
            zu.model = model;
            tracker_.log_request(used->account.id, ar.route.local_key_id, zu,
                                 false, fwd.status_code, fwd.duration_ms, used->key_slot_id);
        }

        if (fwd.success) {
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
            tracker_.log_request(used->account.id, ar.route.local_key_id, zu,
                                 false, fwd.status_code, fwd.duration_ms, used->key_slot_id);
            res.status = fwd.status_code;
            if (fwd.is_timeout) {
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
    } else {
        if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
            ir::ChatResponse cResp;
            bool parsed = false;
            try {
                parsed = upstream_codec->parse_response(json::parse(fwd.body),
                                                        cResp, perr);
            } catch (...) {
                parsed = false;
            }
            if (parsed) {
                auto usage_info = usage_from_ir(cResp.usage, used_upstream_fmt);
                usage_info.model = model;
                tracker_.log_request(used->account.id, ar.route.local_key_id,
                                     usage_info, false,
                                     fwd.status_code, fwd.duration_ms, used->key_slot_id);
                res.status = fwd.status_code;
                res.set_content(harness_codec.serialize_response(cResp).dump(),
                                "application/json");
            } else {
                auto fb = parse_usage_for_format(ir::to_string(used_upstream_fmt),
                                                 fwd.body);
                if (fb.has_value()) {
                    if (fb->model.empty()) fb->model = model;
                    tracker_.log_request(used->account.id, ar.route.local_key_id,
                                         *fb, false, fwd.status_code,
                                         fwd.duration_ms, used->key_slot_id);
                } else {
                    // 2xx but no usable usage — log the attempt (zero tokens).
                    UsageTracker::UsageInfo zu;
                    zu.model = model;
                    tracker_.log_request(used->account.id, ar.route.local_key_id,
                                         zu, false, fwd.status_code,
                                         fwd.duration_ms, used->key_slot_id);
                }
                res.status = fwd.status_code;
                res.set_content(fwd.body, "application/json");
            }
        } else {
            // Non-2xx / upstream failure: record the failed attempt (zero
            // tokens, truthful status — 504 on timeout, else 502/upstream).
            UsageTracker::UsageInfo zu;
            zu.model = model;
            tracker_.log_request(used->account.id, ar.route.local_key_id,
                                 zu, false, fwd.status_code, fwd.duration_ms, used->key_slot_id);
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

    tracker_.mark_key_used(ar.route.local_key_id);

    auto t1 = std::chrono::steady_clock::now();
    int proxy_ttft = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    tracker_.log_perf_event(model, fwd.ttft_ms, proxy_ttft,
                            fwd.status_code, fwd.status_code >= 400,
                            concurrent_count);

    db_.request_end(inflight_id);
    gate_.release(used->key_slot_id);
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
    res.set_chunked_content_provider(
        "text/event-stream",
        [this, cands, start, session_id, local_key_id, harness, resolved_model,
         request_body = req.body, content_type, parsed_request, t0, &res,
         client_sock = req.client_socket](size_t, httplib::DataSink &sink) -> bool {
            const FormatCodec &out_codec = codecs_.get(harness);
            int inflight_id = -1;
            int concurrent_count = 0;
            const UpstreamCandidate *used = nullptr;
            UpstreamClient::ForwardResult final_result;
            bool committed = false;
            bool last_timeout = false;
            int last_status = 429;
            int last_account_id = 0;
            int last_slot_id = 0;
            int last_duration_ms = 0;
            std::string last_body;
            bool first_response = true;
            std::chrono::steady_clock::time_point first_response_at;

            auto write_to_sink = [&](const std::string &data) -> bool {
                if (data.empty()) return true;
                bool ok = sink.write(data.data(), data.size());
                if (ok) committed = true;
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

            for (size_t attempt = 0; attempt < cands.size(); ++attempt) {
                const auto &candidate = cands[(start + attempt) % cands.size()];
                if (gate_.in_cooldown(candidate.key_slot_id)) continue;
                if (!gate_.acquire(candidate.key_slot_id, candidate.account.max_concurrency)) continue;

                if (inflight_id < 0) {
                    inflight_id = db_.request_start(local_key_id, candidate.account.id,
                                                    candidate.upstream_model, true);
                    concurrent_count = db_.get_in_flight_count();
                }

                const auto upstream = ir::parse_api_format(candidate.account.api_format);
                const bool passthrough = harness == upstream;
                const bool filter_thinking = passthrough && upstream == ir::ApiFormat::OpenAI;
                auto target = resolve_upstream_target(
                    candidate.account.api_format, candidate.account.base_url,
                    candidate.account.endpoint_path, candidate.account.auth_header,
                    timeout_config_for(db_, harness));
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
                ThinkStreamFilter think_filter;
                bool has_reasoning = false;
                if (!passthrough) {
                    parser = codecs_.get(upstream).make_stream_parser();
                    emitter = out_codec.make_stream_emitter();
                }
                auto on_event = [&](const ir::StreamEvent &event) -> bool {
                    return emitter->emit(event, write_to_sink);
                };
                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    if (first_response) {
                        first_response = false;
                        first_response_at = std::chrono::steady_clock::now();
                    }
                    if (!passthrough) return parser->feed(data, len, on_event);
                    if (!filter_thinking) return write_to_sink(std::string(data, len));
                    if (!has_reasoning && sse_chunk_has_reasoning(data, len)) has_reasoning = true;
                    if (has_reasoning) return write_to_sink(std::string(data, len));
                    std::string filtered = think_filter.feed(data, len);
                    return filtered.empty() || write_to_sink(filtered);
                };

                AbortGuard abort;
                auto monitor = spawn_client_monitor(client_sock, abort);
                auto result = upstream_.forward(
                    "POST", candidate.account.base_url, candidate.key, target.path, body,
                    passthrough ? content_type : "application/json", on_chunk, target.opts,
                    [&](int fd) { abort.upstream_fd.store(fd, std::memory_order_relaxed); });
                abort.running.store(false, std::memory_order_release);
                if (monitor.joinable()) monitor.join();

                if (result.success && result.status_code >= 200 && result.status_code < 300) {
                    if (!passthrough) {
                        parser->finish(on_event);
                        emitter->finish(write_to_sink);
                    }
                    final_result = std::move(result);
                    used = &candidate;
                    break;
                }

                if (result.status_code == 429 && candidate.account.account_type == "plan")
                    gate_.mark_cooldown(candidate.key_slot_id);
                last_timeout = result.is_timeout;
                last_status = result.status_code;
                last_account_id = candidate.account.id;
                last_slot_id = candidate.key_slot_id;
                last_duration_ms = result.duration_ms;
                last_body = result.body;
                final_result = std::move(result);
                gate_.release(candidate.key_slot_id);

                // A parser can receive bytes without emitting a harness event;
                // only a successful sink.write commits this client response.
                if (committed || !(last_status == 429 || last_status >= 500)) break;
            }

            if (inflight_id >= 0) db_.request_end(inflight_id);
            if (used) {
                auto usage = UsageTracker::parse_stream_usage(
                    ir::to_string(ir::parse_api_format(used->account.api_format)), final_result.body);
                if (!usage) {
                    UsageTracker::UsageInfo zero;
                    zero.model = resolved_model;
                    usage = zero;
                }
                usage->model = resolved_model;
                tracker_.log_request(used->account.id, local_key_id, *usage, true,
                                     final_result.status_code, final_result.duration_ms, used->key_slot_id);
                tracker_.mark_key_used(local_key_id);
                const int proxy_ttft = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        (first_response ? std::chrono::steady_clock::now() : first_response_at) - t0).count());
                tracker_.log_perf_event(resolved_model, final_result.ttft_ms, proxy_ttft,
                                        final_result.status_code, false, concurrent_count);
                if (affinity_.binding_changed(session_id, used->key_slot_id))
                    db_.log_session_key(session_id, used->account.id, used->key_slot_id);
                gate_.release(used->key_slot_id);
                sink.done();
                return true;
            }

            if (client_socket_gone(client_sock)) return false;
            const int final_status = last_timeout ? 504 : last_status;
            res.status = final_status;
            json normalized = last_timeout
                ? timeout_error_body(final_result.timeout_secs)
                : json{{"message", last_body.empty()
                    ? "All upstream accounts are busy, cooling down, or failed" : last_body},
                       {"type", final_status == 429 ? "rate_limit_error" : "upstream_error"},
                       {"code", final_status}};
            if (last_account_id) {
                UsageTracker::UsageInfo zero;
                zero.model = resolved_model;
                tracker_.log_request(last_account_id, local_key_id, zero, true,
                                     final_status, last_duration_ms, last_slot_id);
            }
            emit_error(normalized);
            sink.done();
            return true;
        },
        nullptr);
    res.set_deferred_chunked_headers();
}

#if 0  // Replaced by handle_streaming; retained temporarily for reference.
// ── handle_passthrough (streaming only) ─────────────────────────────────

/// Streaming passthrough for one already-picked candidate.  Non-streaming
/// requests are handled by handle_chat_request's candidate loop instead.
void ProxyServer::handle_passthrough(const UpstreamCandidate &cand,
                                     int local_key_id,
                                     ir::ApiFormat upstream,
                                     const std::string &resolved_model,
                                     const httplib::Request &req,
                                     httplib::Response &res,
                                     std::chrono::steady_clock::time_point t0) {
    std::string body = req.body;
    apply_body_model(body, cand.upstream_model);

    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    int inflight_id = db_.request_start(local_key_id, cand.account.id,
                                        cand.upstream_model, true);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] passthrough(%s) streaming request from key_id=%d "
                    "to account=%d model=%s (concurrent=%d, inflight_id=%d)\n",
            ir::to_string(upstream).c_str(), local_key_id, cand.account.id,
            cand.upstream_model.c_str(), concurrent_count, inflight_id);

    auto target = resolve_upstream_target(
        cand.account.api_format, cand.account.base_url,
        cand.account.endpoint_path, cand.account.auth_header,
        timeout_config_for(db_, upstream));
    bool think_filter = (upstream == ir::ApiFormat::OpenAI);
    std::string upstream_fmt = ir::to_string(upstream);

    res.set_chunked_content_provider(
        "text/event-stream",
        [this, cand, target, body, content_type, resolved_model, upstream, t0,
         concurrent_count, inflight_id, think_filter, upstream_fmt,
         local_key_id, client_sock = req.client_socket](
            size_t /*offset*/, httplib::DataSink &sink) -> bool {

            std::string accumulated;
            bool first_response = true;
            std::chrono::steady_clock::time_point t_first_resp;
            ThinkStreamFilter filter;
            bool has_reasoning = false;

            auto write_to_sink = [&](const char *d, size_t n) -> bool {
                return sink.write(d, n);
            };

            auto on_chunk = [&](const char *data, size_t len) -> bool {
                if (first_response) {
                    t_first_resp = std::chrono::steady_clock::now();
                    first_response = false;
                }
                accumulated.append(data, len);
                if (!think_filter) return write_to_sink(data, len);
                if (!has_reasoning && sse_chunk_has_reasoning(data, len))
                    has_reasoning = true;
                if (has_reasoning) return write_to_sink(data, len);
                std::string filtered = filter.feed(data, len);
                if (!filtered.empty())
                    return write_to_sink(filtered.data(), filtered.size());
                return true;
            };

            AbortGuard abort;
            auto monitor = spawn_client_monitor(client_sock, abort);
            auto fwd = upstream_.forward(
                "POST", cand.account.base_url, cand.key,
                target.path, body, content_type, on_chunk, target.opts,
                [&](int fd) {
                    abort.upstream_fd.store(fd, std::memory_order_relaxed);
                });
            abort.running.store(false, std::memory_order_release);
            if (monitor.joinable()) monitor.join();

            // plan keys cool down for 5h when the upstream rate-limits them.
            if (fwd.status_code == 429 && cand.account.account_type == "plan") {
                fprintf(stderr, "[Proxy] key slot %d (account %d %s) plan "
                                "rate-limited (429), cooling down 5h\n",
                        cand.key_slot_id, cand.account.id, cand.account.name.c_str());
                gate_.mark_cooldown(cand.key_slot_id);
            }

            auto usage = UsageTracker::parse_stream_usage(upstream_fmt, fwd.body);
            if (usage.has_value()) {
                tracker_.log_request(cand.account.id, local_key_id,
                                     *usage, true, fwd.status_code,
                                     fwd.duration_ms, cand.key_slot_id);
            } else {
                fprintf(stderr, "[Proxy] Warning: could not parse usage "
                                "from streaming response\n");
                // No usage — still log the attempt (zero tokens, truthful
                // status: 504 on timeout, else the upstream status).
                UsageTracker::UsageInfo zu;
                zu.model = resolved_model;
                tracker_.log_request(cand.account.id, local_key_id,
                                     zu, true, fwd.status_code,
                                     fwd.duration_ms, cand.key_slot_id);
            }

            tracker_.mark_key_used(local_key_id);

            int proxy_ttft = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    first_response ? std::chrono::steady_clock::now() - t0
                                   : t_first_resp - t0).count());
            tracker_.log_perf_event(resolved_model, fwd.ttft_ms, proxy_ttft,
                                    fwd.status_code, fwd.status_code >= 400,
                                    concurrent_count);

            db_.request_end(inflight_id);
            gate_.release(cand.key_slot_id);
            if (fwd.is_timeout) {
                // The monitor thread shutdown()s the upstream socket when
                // the client disconnects, which surfaces as a read error
                // (is_timeout) — the client is already gone, no error frame
                // needed.
                if (client_socket_gone(client_sock)) return false;
                // Don't drop the connection silently — emit a terminal
                // error event so the client sees "upstream timeout" and
                // can prompt the user to retry.
                fprintf(stderr, "[Proxy] Upstream timeout, sending error "
                                "event to client (inflight=%d)\n", inflight_id);
                std::string frame = timeout_sse_frame(upstream, fwd.timeout_secs);
                write_to_sink(frame.data(), frame.size());
                sink.done();
                return true;
            }
            sink.done();
            return true;
        },
        /* user_data */ nullptr);
}

// ── handle_converted ─────────────────────────────────────────────────────
// ── handle_converted (streaming only) ────────────────────────────────────

/// Streaming format-conversion path for one already-picked candidate.
/// Non-streaming requests are handled by handle_chat_request's candidate
/// loop instead (the request is parsed there once, up front).
void ProxyServer::handle_converted(const UpstreamCandidate &cand,
                                   int local_key_id,
                                   ir::ApiFormat harness, ir::ApiFormat upstream,
                                   const std::string &resolved_model,
                                   const httplib::Request &req,
                                   httplib::Response &res,
                                   std::chrono::steady_clock::time_point t0) {
    const FormatCodec &harness_codec = codecs_.get(harness);
    const FormatCodec &upstream_codec = codecs_.get(upstream);
    const FormatCodec *harness_codec_ptr = &harness_codec;
    const FormatCodec *upstream_codec_ptr = &upstream_codec;

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

    cReq.model = resolved_model;
    std::string upstream_body = upstream_codec.serialize_request(cReq).dump();

    auto target = resolve_upstream_target(
        cand.account.api_format, cand.account.base_url,
        cand.account.endpoint_path, cand.account.auth_header,
        timeout_config_for(db_, harness));

    int inflight_id = db_.request_start(local_key_id, cand.account.id,
                                        cReq.model, true);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] convert %s→%s streaming request from key_id=%d "
                    "to account=%d model=%s (concurrent=%d, inflight_id=%d)\n",
            ir::to_string(harness).c_str(), ir::to_string(upstream).c_str(),
            local_key_id, cand.account.id, cReq.model.c_str(),
            concurrent_count, inflight_id);

    auto model_copy = cReq.model;
    std::string upstream_fmt = ir::to_string(upstream);

    res.set_chunked_content_provider(
        "text/event-stream",
        [this, cand, target, upstream_body, model_copy, upstream_fmt, harness,
         upstream, t0, concurrent_count, inflight_id, harness_codec_ptr,
         upstream_codec_ptr, local_key_id, client_sock = req.client_socket](
            size_t /*offset*/, httplib::DataSink &sink) -> bool {

            auto parser = upstream_codec_ptr->make_stream_parser();
            auto emitter = harness_codec_ptr->make_stream_emitter();
            ir::Usage last_usage;
            std::string accumulated;
            bool first_response = true;
            std::chrono::steady_clock::time_point t_first_resp;

            auto sink_bridge = [&](const std::string &c) -> bool {
                return sink.write(c.data(), c.size());
            };
            auto on_event = [&](const ir::StreamEvent &ev) -> bool {
                if (ev.type == ir::StreamEventType::UsageEvent)
                    last_usage = ev.usage;
                return emitter->emit(ev, sink_bridge);
            };
            auto on_chunk = [&](const char *data, size_t len) -> bool {
                if (first_response) {
                    t_first_resp = std::chrono::steady_clock::now();
                    first_response = false;
                }
                accumulated.append(data, len);
                return parser->feed(data, len, on_event);
            };

            AbortGuard abort;
            auto monitor = spawn_client_monitor(client_sock, abort);
            auto fwd = upstream_.forward(
                "POST", cand.account.base_url, cand.key,
                target.path, upstream_body, "application/json",
                on_chunk, target.opts,
                [&](int fd) {
                    abort.upstream_fd.store(fd, std::memory_order_relaxed);
                });
            abort.running.store(false, std::memory_order_release);
            if (monitor.joinable()) monitor.join();

            // plan keys cool down for 5h when the upstream rate-limits them.
            if (fwd.status_code == 429 && cand.account.account_type == "plan") {
                fprintf(stderr, "[Proxy] key slot %d (account %d %s) plan "
                                "rate-limited (429), cooling down 5h\n",
                        cand.key_slot_id, cand.account.id, cand.account.name.c_str());
                gate_.mark_cooldown(cand.key_slot_id);
            }

            if ((fwd.status_code >= 400 || !fwd.success) && !fwd.is_timeout) {
                ir::StreamEvent err_ev;
                err_ev.type = ir::StreamEventType::ErrorEvent;
                json normalized;
                try {
                    normalized = upstream_codec_ptr->parse_error_body(
                        json::parse(fwd.body));
                } catch (...) {
                    normalized = json{{"message", fwd.error}};
                }
                err_ev.extra["error"] = normalized;
                emitter->emit(err_ev, sink_bridge);
                emitter->finish(sink_bridge);
            } else {
                parser->finish(on_event);
                emitter->finish(sink_bridge);
            }

            // Log usage (stream events; fall back to accumulated-body parse).
            if (last_usage.prompt_tokens == 0 && last_usage.completion_tokens == 0) {
                auto fb = UsageTracker::parse_stream_usage(upstream_fmt, accumulated);
                if (fb.has_value()) {
                    last_usage.prompt_tokens = fb->prompt_tokens;
                    last_usage.completion_tokens = fb->completion_tokens;
                    last_usage.cache_read_tokens = fb->cache_read_tokens;
                    last_usage.cache_creation_tokens = fb->cache_creation_tokens;
                    last_usage.total_tokens = fb->total_tokens;
                }
            }
            auto usage_info = usage_from_ir(last_usage, upstream);
            usage_info.model = model_copy;
            tracker_.log_request(cand.account.id, local_key_id,
                                 usage_info, true, fwd.status_code,
                                 fwd.duration_ms, cand.key_slot_id);

            tracker_.mark_key_used(local_key_id);

            int proxy_ttft = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    first_response ? std::chrono::steady_clock::now() - t0
                                   : t_first_resp - t0).count());
            tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                                    fwd.status_code, fwd.status_code >= 400,
                                    concurrent_count);

            db_.request_end(inflight_id);
            gate_.release(cand.key_slot_id);
            if (fwd.is_timeout) {
                // The monitor thread shutdown()s the upstream socket when
                // the client disconnects, which surfaces as a read error
                // (is_timeout) — the client is already gone, no error frame
                // needed.
                if (client_socket_gone(client_sock)) return false;
                // Don't drop the connection silently — emit a terminal
                // error event (in the harness wire format) so the client
                // sees "upstream timeout" and can prompt the user to retry.
                fprintf(stderr, "[Proxy] Upstream timeout, sending error "
                                "event to client (inflight=%d)\n", inflight_id);
                ir::StreamEvent err_ev;
                err_ev.type = ir::StreamEventType::ErrorEvent;
                err_ev.extra["error"] = timeout_error_body(fwd.timeout_secs);
                emitter->emit(err_ev, sink_bridge);
                if (harness == ir::ApiFormat::OpenAI)
                    sink_bridge("data: [DONE]\n\n");
                emitter->finish(sink_bridge);
                sink.done();
                return true;
            }
            sink.done();
            return true;
        },
        /* user_data */ nullptr);
}

#endif

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

    ForwardOptions mopts;
    mopts.non_streaming_timeout =
        timeout_config_for(db_, ir::ApiFormat::OpenAI).non_streaming_timeout;
    auto fwd = upstream_.forward("GET", ar.route.base_url,
                                 ar.route.upstream_key,
                                 "/models", "", "application/json", nullptr,
                                 mopts);

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
    auto cands = resolve_candidates(db_, ar.route, req_model);
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
    int inflight_id = -1;
    int concurrent_count = 0;
    UpstreamClient::ForwardResult fwd;
    const UpstreamCandidate *used = nullptr;

    for (size_t i = 0; i < cands.size(); ++i) {
        const UpstreamCandidate &c = cands[i];
        if (gate_.in_cooldown(c.key_slot_id)) continue;
        if (!gate_.acquire(c.key_slot_id, c.account.max_concurrency)) continue;

        if (inflight_id < 0) {
            inflight_id = db_.request_start(ar.route.local_key_id,
                                            c.account.id,
                                            c.upstream_model, false);
            concurrent_count = db_.get_in_flight_count();
        }

        fprintf(stderr, "[Proxy] embedding request from key_id=%d to account=%d "
                        "model=%s (concurrent=%d, inflight_id=%d)\n",
                ar.route.local_key_id, c.account.id,
                c.upstream_model.c_str(), concurrent_count, inflight_id);

        std::string eb = req.body;
        apply_body_model(eb, c.upstream_model);
        ForwardOptions eopts;
        eopts.non_streaming_timeout =
            timeout_config_for(db_, ir::ApiFormat::OpenAI).non_streaming_timeout;
        fwd = upstream_.forward(
            "POST", c.account.base_url, c.key,
            "/embeddings", eb, content_type, nullptr, eopts);

        if (fwd.status_code == 429 && c.account.account_type == "plan") {
            fprintf(stderr, "[Proxy] key slot %d (account %d %s) plan "
                            "rate-limited (429), cooling down 5h\n",
                    c.key_slot_id, c.account.id, c.account.name.c_str());
            gate_.mark_cooldown(c.key_slot_id);
        }

        bool retryable = (fwd.status_code == 429 || fwd.status_code >= 500) &&
                         i + 1 < cands.size();
        if (retryable) {
            fprintf(stderr, "[Proxy] upstream %d (%s) failed (%d), trying "
                            "next candidate\n",
                    c.account.id, c.account.name.c_str(), fwd.status_code);
            gate_.release(c.key_slot_id);
            continue;
        }
        used = &c;
        break;
    }

    if (!used) {
        if (inflight_id >= 0) db_.request_end(inflight_id);
        if (fwd.is_timeout) {
            // Every candidate timed out — a timeout error, not a busy signal.
            res.status = 504;
            res.set_header("Connection", "close");
            res.set_content(json{{"error",
                                  timeout_error_body(fwd.timeout_secs)}}.dump(),
                            "application/json");
        } else {
            res.status = 429;
            res.set_content(json_error("All upstream accounts are busy, cooling "
                                       "down, or failed", 429),
                            "application/json");
        }
        return;
    }

    // ── Check if client disconnected while waiting for upstream ──
    if (req.client_socket != INVALID_SOCKET) {
        struct pollfd pfd;
        pfd.fd = req.client_socket;
        pfd.events = POLLIN;
        pfd.revents = 0;
        if (poll(&pfd, 1, 0) > 0 &&
            (pfd.revents & (POLLHUP | POLLERR | POLLRDHUP))) {
            fprintf(stderr, "[Proxy] Client gone (embeddings), drop response "
                    "(inflight=%d, model=%s)\n", inflight_id, req_model.c_str());
            db_.request_end(inflight_id);
            gate_.release(used->key_slot_id);
            return;
        }
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    // Parse usage
    auto usage = UsageTracker::parse_usage(fwd.body);
    if (usage.has_value()) {
        tracker_.log_request(used->account.id,
                             ar.route.local_key_id,
                             *usage, false, fwd.status_code,
                             fwd.duration_ms, used->key_slot_id);
    } else {
        fprintf(stderr, "[Proxy] Warning: could not parse usage "
                        "from embedding response, model=%s\n",
                        req_model.c_str());
    }

    tracker_.mark_key_used(ar.route.local_key_id);

    // ── Perf ──
    auto t1 = std::chrono::steady_clock::now();
    int proxy_ttft = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    tracker_.log_perf_event(req_model, fwd.ttft_ms, proxy_ttft,
                            fwd.status_code, fwd.status_code >= 400,
                            concurrent_count);

    db_.request_end(inflight_id);
    gate_.release(used->key_slot_id);

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
        res.set_content(j.dump(), "application/json");
    });
}
