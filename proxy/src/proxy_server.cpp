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

/// Build a JSON error response.
static std::string json_error(const std::string &msg, int code) {
    json j;
    j["error"] = {{"message", msg}, {"type", "auth_error"}, {"code", code}};
    return j.dump();
}

/// Unified upstream-timeout error object returned to the client.
/// type=timeout_error + code 504 — clients (opencode, Claude Code, SDK retry
/// middlewares) recognize this as a retryable error instead of an opaque
/// connection drop.
static json timeout_error_body() {
    return json{{"message",
                 "Upstream timeout: no response within 100s. Please retry."},
                {"type", "timeout_error"},
                {"code", 504}};
}

/// SSE error frame for the passthrough streaming path (no codec available).
/// Emits a terminal error event in the client's wire format instead of
/// silently dropping the connection, so the client knows the upstream never
/// replied and can prompt the user to retry.
static std::string timeout_sse_frame(ir::ApiFormat fmt) {
    json err = timeout_error_body();
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
static void resolve_upstream_path(const Router::RouteResult &route,
                                  std::string &out_path,
                                  bool &out_path_is_full) {
    if (!route.endpoint_path.empty()) {
        out_path = route.endpoint_path;
        out_path_is_full = true;
        return;
    }
    out_path_is_full = false;

    // Extract the base_url path component to detect a trailing "/v1"
    // (OpenAI-style base URLs) and avoid "/v1/v1/messages" double-append.
    std::string base_path;
    size_t scheme_end = route.base_url.find("://");
    if (scheme_end != std::string::npos) {
        scheme_end += 3;
        size_t path_start = route.base_url.find('/', scheme_end);
        if (path_start != std::string::npos)
            base_path = route.base_url.substr(path_start);
    }

    if (route.api_format == "anthropic") {
        if (base_path.size() >= 3 &&
            base_path.compare(base_path.size() - 3, 3, "/v1") == 0)
            out_path = "/messages";
        else
            out_path = "/v1/messages";
    } else if (route.api_format == "openai_responses") {
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

/// Resolve the effective upstream model for a request: strip the Claude Code
/// `[1m]`/`[1M]` context-window marker (upstreams reject it) and, for
/// aggregate accounts, resolve the real upstream account + model via the
/// account's entry list.  On aggregate resolution the route is overwritten
/// with the real account's connection details so billing is attributed to
/// the real upstream account.  Returns false when an aggregate account has
/// no entry matching the model.
static bool resolve_upstream_model(Database &db, Router::RouteResult &route,
                                   std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    if (!route.is_aggregate) return true;

    auto target = db.resolve_aggregate(route.account_id, model);
    if (!target.has_value()) {
        fprintf(stderr, "[Proxy] aggregate account %d: no entry for model %s\n",
                route.account_id, model.c_str());
        return false;
    }
    auto real = db.get_account(target->upstream_account_id);
    if (!real.has_value()) {
        fprintf(stderr, "[Proxy] aggregate account %d: upstream account %d missing\n",
                route.account_id, target->upstream_account_id);
        return false;
    }
    fprintf(stderr, "[Proxy] aggregate %d: %s → account %d (%s) model %s\n",
            route.account_id, model.c_str(), real->id, real->name.c_str(),
            target->upstream_model.c_str());
    route.upstream_key = real->upstream_key;
    route.base_url = real->base_url;
    route.api_format = real->api_format;
    route.endpoint_path = real->endpoint_path;
    route.auth_header = real->auth_header;
    route.account_id = real->id;
    route.is_aggregate = false;  // resolved — treat as a plain account below
    model = target->upstream_model;
    return true;
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
static UpstreamTarget resolve_upstream_target(const Router::RouteResult &route) {
    UpstreamTarget t;
    resolve_upstream_path(route, t.path, t.opts.path_is_full);
    // Outbound auth scheme: `auto` (the dashboard default) derives from the
    // upstream wire format — Anthropic-native uses x-api-key + anthropic-version,
    // everything else uses Authorization: Bearer.  Explicit `bearer` /
    // `x-api-key` remain as overrides for relays that need them.
    const std::string &ah = route.auth_header;
    if (ah == "auto" || ah.empty())
        t.opts.auth_scheme = (route.api_format == "anthropic") ? "x-api-key" : "bearer";
    else
        t.opts.auth_scheme = ah;
    return t;
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

// ── handle_chat_request ──────────────────────────────────────────────────

/// Entry point for /v1/chat/completions, /v1/messages and /v1/responses.
/// The harness format comes from the incoming request URL path; when it matches
/// the account's api_format we use the passthrough fast path, otherwise we
/// convert via the IR codecs.
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
    // split, which depends on the real account's api_format.  For plain
    // accounts this only strips the `[1m]`/`[1M]` marker (no-op otherwise).
    std::string model = extract_model(req.body);
    if (!resolve_upstream_model(db_, ar.route, model)) {
        res.status = 400;
        res.set_content(json_error("Model '" + model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }

    ir::ApiFormat harness = harness_format_from_path(req.path);
    ir::ApiFormat upstream = ir::parse_api_format(ar.route.api_format);

    if (harness == upstream) {
        handle_passthrough(ar.route, upstream, model, req, res, t0);
    } else {
        handle_converted(ar.route, harness, upstream, model, req, res, t0);
    }
}

// ── handle_passthrough ───────────────────────────────────────────────────

void ProxyServer::handle_passthrough(Router::RouteResult &route,
                                     ir::ApiFormat upstream,
                                     const std::string &resolved_model,
                                     const httplib::Request &req,
                                     httplib::Response &res,
                                     std::chrono::steady_clock::time_point t0) {
    std::string body = req.body;
    std::string req_model = resolved_model;
    apply_body_model(body, req_model);

    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";
    bool is_stream = is_streaming_request(body);

    int inflight_id = db_.request_start(route.local_key_id, route.account_id,
                                        req_model, is_stream);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] passthrough(%s) %s request from key_id=%d to account=%d "
                    "model=%s (concurrent=%d, inflight_id=%d)\n",
            ir::to_string(upstream).c_str(), is_stream ? "streaming" : "non-streaming",
            route.local_key_id, route.account_id, req_model.c_str(),
            concurrent_count, inflight_id);

    auto model_copy = req_model;
    auto target = resolve_upstream_target(route);
    bool think_filter = (upstream == ir::ApiFormat::OpenAI);
    std::string upstream_fmt = ir::to_string(upstream);

    if (is_stream) {
        // ── Streaming path ──────────────────────────────────────────
        res.set_chunked_content_provider(
            "text/event-stream",
            [this, route, target, body, content_type, model_copy, upstream, t0,
             concurrent_count, inflight_id, think_filter, upstream_fmt,
             client_sock = req.client_socket](
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
                    "POST", route.base_url, route.upstream_key,
                    target.path, body, content_type, on_chunk, target.opts,
                    [&](int fd) {
                        abort.upstream_fd.store(fd, std::memory_order_relaxed);
                    });
                abort.running.store(false, std::memory_order_release);
                if (monitor.joinable()) monitor.join();

                auto usage = UsageTracker::parse_stream_usage(upstream_fmt, fwd.body);
                if (usage.has_value()) {
                    tracker_.log_request(route.account_id, route.local_key_id,
                                         *usage, true, fwd.status_code,
                                         fwd.duration_ms);
                } else {
                    fprintf(stderr, "[Proxy] Warning: could not parse usage "
                                    "from streaming response\n");
                }

                tracker_.mark_key_used(route.local_key_id);

                int proxy_ttft = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_response ? std::chrono::steady_clock::now() - t0
                                       : t_first_resp - t0).count());
                tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                                        fwd.status_code, fwd.status_code >= 400,
                                        concurrent_count);

                db_.request_end(inflight_id);
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
                    std::string frame = timeout_sse_frame(upstream);
                    write_to_sink(frame.data(), frame.size());
                    sink.done();
                    return true;
                }
                sink.done();
                return true;
            },
            /* user_data */ nullptr);
        return;
    }

    // ── Non-streaming path ──────────────────────────────────────────
    AbortGuard abort;
    auto monitor = spawn_client_monitor(req.client_socket, abort);
    auto fwd = upstream_.forward(
        "POST", route.base_url, route.upstream_key,
        target.path, body, content_type, nullptr, target.opts,
        [&](int fd) {
            abort.upstream_fd.store(fd, std::memory_order_relaxed);
        });
    abort.running.store(false, std::memory_order_release);
    if (monitor.joinable()) monitor.join();

    if (client_disconnected(req, inflight_id, req_model)) {
        db_.request_end(inflight_id);
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    auto usage = parse_usage_for_format(upstream_fmt, fwd.body);
    if (usage.has_value()) {
        tracker_.log_request(route.account_id, route.local_key_id,
                             *usage, false, fwd.status_code, fwd.duration_ms);
    } else {
        fprintf(stderr, "[Proxy] Warning: could not parse usage "
                        "from non-streaming response, model=%s\n",
                req_model.c_str());
    }

    tracker_.mark_key_used(route.local_key_id);

    auto t1 = std::chrono::steady_clock::now();
    int proxy_ttft = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    tracker_.log_perf_event(req_model, fwd.ttft_ms, proxy_ttft,
                            fwd.status_code, fwd.status_code >= 400,
                            concurrent_count);

    db_.request_end(inflight_id);

    if (fwd.success) {
        if (think_filter)
            res.set_content(sanitize_response_body(fwd.body), "application/json");
        else
            res.set_content(fwd.body, "application/json");
        res.status = fwd.status_code;
    } else {
        res.status = fwd.status_code;
        if (fwd.is_timeout) {
            // Explicit, retryable timeout error instead of a generic error.
            res.set_header("Connection", "close");
            res.set_content(json{{"error", timeout_error_body()}}.dump(),
                            "application/json");
        } else {
            res.set_content(json_error("Upstream error: " + fwd.error,
                                       fwd.status_code),
                            "application/json");
        }
    }
}

// ── handle_converted ─────────────────────────────────────────────────────

void ProxyServer::handle_converted(Router::RouteResult &route,
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

    auto target = resolve_upstream_target(route);
    bool is_stream = cReq.stream;

    int inflight_id = db_.request_start(route.local_key_id, route.account_id,
                                        cReq.model, is_stream);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] convert %s→%s %s request from key_id=%d to account=%d "
                    "model=%s (concurrent=%d, inflight_id=%d)\n",
            ir::to_string(harness).c_str(), ir::to_string(upstream).c_str(),
            is_stream ? "streaming" : "non-streaming",
            route.local_key_id, route.account_id, cReq.model.c_str(),
            concurrent_count, inflight_id);

    auto model_copy = cReq.model;
    std::string upstream_fmt = ir::to_string(upstream);

    if (is_stream) {
        // ── Streaming path ──────────────────────────────────────────
        res.set_chunked_content_provider(
            "text/event-stream",
            [this, route, target, upstream_body, model_copy, upstream_fmt, harness, upstream, t0,
             concurrent_count, inflight_id, harness_codec_ptr, upstream_codec_ptr,
             client_sock = req.client_socket](
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
                    "POST", route.base_url, route.upstream_key,
                    target.path, upstream_body, "application/json",
                    on_chunk, target.opts,
                    [&](int fd) {
                        abort.upstream_fd.store(fd, std::memory_order_relaxed);
                    });
                abort.running.store(false, std::memory_order_release);
                if (monitor.joinable()) monitor.join();

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
                tracker_.log_request(route.account_id, route.local_key_id,
                                     usage_info, true, fwd.status_code,
                                     fwd.duration_ms);

                tracker_.mark_key_used(route.local_key_id);

                int proxy_ttft = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_response ? std::chrono::steady_clock::now() - t0
                                       : t_first_resp - t0).count());
                tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                                        fwd.status_code, fwd.status_code >= 400,
                                        concurrent_count);

                db_.request_end(inflight_id);
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
                    err_ev.extra["error"] = timeout_error_body();
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
        return;
    }

    // ── Non-streaming path ──────────────────────────────────────────
    AbortGuard abort;
    auto monitor = spawn_client_monitor(req.client_socket, abort);
    auto fwd = upstream_.forward(
        "POST", route.base_url, route.upstream_key,
        target.path, upstream_body, "application/json", nullptr, target.opts,
        [&](int fd) {
            abort.upstream_fd.store(fd, std::memory_order_relaxed);
        });
    abort.running.store(false, std::memory_order_release);
    if (monitor.joinable()) monitor.join();

    if (client_disconnected(req, inflight_id, model_copy)) {
        db_.request_end(inflight_id);
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
        ir::ChatResponse cResp;
        bool parsed = false;
        try {
            parsed = upstream_codec.parse_response(json::parse(fwd.body), cResp, perr);
        } catch (...) {
            parsed = false;
        }
        if (parsed) {
            auto usage_info = usage_from_ir(cResp.usage, upstream);
            usage_info.model = model_copy;
            tracker_.log_request(route.account_id, route.local_key_id,
                                 usage_info, false,
                                 fwd.status_code, fwd.duration_ms);
            res.status = fwd.status_code;
            res.set_content(harness_codec.serialize_response(cResp).dump(),
                            "application/json");
        } else {
            auto fb = parse_usage_for_format(upstream_fmt, fwd.body);
            if (fb.has_value()) {
                if (fb->model.empty()) fb->model = model_copy;
                tracker_.log_request(route.account_id, route.local_key_id,
                                     *fb, false, fwd.status_code, fwd.duration_ms);
            }
            res.status = fwd.status_code;
            res.set_content(fwd.body, "application/json");
        }
    } else {
        json normalized;
        if (fwd.is_timeout) {
            normalized = timeout_error_body();
        } else {
            try {
                normalized = upstream_codec.parse_error_body(json::parse(fwd.body));
            } catch (...) {
                normalized = json{{"message", fwd.error.empty() ? "upstream error" : fwd.error}};
            }
        }
        res.status = (fwd.status_code >= 400) ? fwd.status_code : 502;
        if (fwd.is_timeout) res.set_header("Connection", "close");
        res.set_content(harness_codec.serialize_error_body(normalized).dump(),
                        "application/json");
    }

    tracker_.mark_key_used(route.local_key_id);

    auto t1 = std::chrono::steady_clock::now();
    int proxy_ttft = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
    tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                            fwd.status_code, fwd.status_code >= 400,
                            concurrent_count);

    db_.request_end(inflight_id);
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
    // accepted by the client.
    if (req.has_header("anthropic-version")) {
        res.status = 200;
        res.set_content("{\"models\":[]}", "application/json");
        return;
    }

    // Aggregate accounts expose their entry patterns as the model catalog
    // (plus the [1m]/[1M] aliases the proxy strips before forwarding).
    if (ar.route.is_aggregate) {
        auto patterns = db_.get_aggregate_model_patterns(ar.route.account_id);
        json out = json::array();
        for (const auto &p : patterns) {
            json m = {{"id", p}, {"object", "model"}, {"created", 1},
                      {"owned_by", "token-board"}};
            out.push_back(m);
            json a1 = m; a1["id"] = p + "[1m]"; out.push_back(a1);
            json a2 = m; a2["id"] = p + "[1M]"; out.push_back(a2);
        }
        res.status = 200;
        res.set_content(json{{"object", "list"}, {"data", std::move(out)}}.dump(),
                        "application/json");
        return;
    }

    auto fwd = upstream_.forward("GET", ar.route.base_url,
                                 ar.route.upstream_key,
                                 "/models", "", "application/json", nullptr);

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    if (fwd.success) {
        // OpenAI-compatible clients still get the real upstream list, but
        // augmented with `[1m]`/`[1M]` aliases of every model so the proxy's
        // supported names (which it strips before forwarding) also pass
        // client-side validation.
        try {
            json j = json::parse(fwd.body);
            if (j.contains("data") && j["data"].is_array()) {
                json out = json::array();
                for (auto &m : j["data"]) {
                    if (m.is_object() && m.contains("id") && m["id"].is_string()) {
                        std::string id = m["id"].get<std::string>();
                        out.push_back(m);
                        json a1 = m; a1["id"] = id + "[1m]"; out.push_back(a1);
                        json a2 = m; a2["id"] = id + "[1M]"; out.push_back(a2);
                    } else {
                        out.push_back(m);
                    }
                }
                j["data"] = std::move(out);
            }
            res.status = fwd.status_code;
            res.set_content(j.dump(), "application/json");
        } catch (...) {
            res.status = fwd.status_code;
            res.set_content(fwd.body, "application/json");
        }
    } else {
        res.status = fwd.status_code;
        if (fwd.is_timeout) {
            // Explicit, retryable timeout error instead of a generic error.
            res.set_header("Connection", "close");
            res.set_content(json{{"error", timeout_error_body()}}.dump(),
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

    // 2. Resolve effective upstream model (strip [1m] marker; aggregate routing)
    std::string body = req.body;
    std::string req_model = extract_model(body);
    if (!resolve_upstream_model(db_, ar.route, req_model)) {
        res.status = 400;
        res.set_content(json_error("Model '" + req_model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }
    apply_body_model(body, req_model);

    // 3. Determine content type
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    // Embeddings are always non-streaming
    bool is_stream = false;

    // ── Register in-flight request ──
    int inflight_id = db_.request_start(ar.route.local_key_id,
                                        ar.route.account_id,
                                        req_model, is_stream);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] embedding request from key_id=%d to account=%d model=%s "
                    "(concurrent=%d, inflight_id=%d)\n",
            ar.route.local_key_id, ar.route.account_id,
            req_model.c_str(), concurrent_count, inflight_id);

    // ── Forward ──
    auto fwd = upstream_.forward(
        "POST", ar.route.base_url, ar.route.upstream_key,
        "/embeddings", body, content_type, nullptr);

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
            return;
        }
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    // Parse usage
    auto usage = UsageTracker::parse_usage(fwd.body);
    if (usage.has_value()) {
        tracker_.log_request(ar.route.account_id,
                             ar.route.local_key_id,
                             *usage, false, fwd.status_code,
                             fwd.duration_ms);
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

    if (fwd.success) {
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        res.status = fwd.status_code;
        if (fwd.is_timeout) {
            // Explicit, retryable timeout error instead of a generic error.
            res.set_header("Connection", "close");
            res.set_content(json{{"error", timeout_error_body()}}.dump(),
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
