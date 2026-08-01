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

#include <chrono>
#include <cctype>
#include <cstdio>
#include <poll.h>
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

/// Exponential backoff delay for retry attempt N (1-based index).
/// Attempt 1→2: 500ms, Attempt 2→3: 1500ms.
static void retry_backoff(int attempt) {
    if (attempt == 1) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
    } else if (attempt >= 2) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1500));
    }
}

/// Shell-style glob match: supports * (any chars) and ? (single char).
/// Case-insensitive. Used for model-mapping-template pattern matching.
static bool glob_match(const std::string &pattern, const std::string &text) {
    size_t pi = 0, mi = 0;
    size_t star_pos = std::string::npos;
    size_t match_pos = 0;

    while (mi < text.size()) {
        if (pi < pattern.size() &&
            (pattern[pi] == '?' ||
             tolower(pattern[pi]) == tolower(text[mi]))) {
            ++pi; ++mi;
        } else if (pi < pattern.size() && pattern[pi] == '*') {
            star_pos = pi; match_pos = mi; ++pi;
        } else if (star_pos != std::string::npos) {
            pi = star_pos + 1; match_pos++; mi = match_pos;
        } else {
            return false;
        }
    }
    while (pi < pattern.size() && pattern[pi] == '*') ++pi;
    return pi == pattern.size();
}

/// Retry helper: calls `do_forward()` up to 3 times with exponential
/// backoff and 4xx-aware early termination.  `is_streaming` controls the
/// log label.  Returns the last ForwardResult with `retries` populated.
template <typename F>
static UpstreamClient::ForwardResult
forward_with_retry(F &&do_forward, bool is_streaming) {
    const char *label = is_streaming ? "streaming" : "non-streaming";
    UpstreamClient::ForwardResult fwd;
    for (int attempt = 0; attempt < 3; attempt++) {
        if (attempt > 0) {
            fprintf(stderr, "[Proxy] Retry %d/3 for %s request "
                            "(backoff %dms, last error: %s)\n",
                    attempt + 1, label,
                    attempt == 1 ? 500 : 1500,
                    fwd.error.c_str());
        }
        fwd = do_forward();
        fwd.retries = attempt;
        if (fwd.success) break;
        // Don't retry client errors (4xx)
        if (!UpstreamClient::is_retryable(fwd.status_code)) {
            fprintf(stderr, "[Proxy] Not retrying: status %d is a client error\n",
                    fwd.status_code);
            break;
        }
        retry_backoff(attempt);
    }
    return fwd;
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
    std::string auth = req.has_header("Authorization")
                          ? req.get_header_value("Authorization")
                          : "";
    std::string local_key;
    if (auth.rfind("Bearer ", 0) == 0)
        local_key = auth.substr(7);

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

// ── Model-mapping helpers ────────────────────────────────────────────────

/// Pure model lookup via the key's mapping template.  Returns the mapped model
/// or the input unchanged.  Used by the conversion pipeline (operates on the IR
/// model) and by the passthrough path.  Any `[1m]` context-window marker is
/// stripped before forwarding (upstreams reject it).
static std::string resolve_upstream_model(Database &db, int local_key_id,
                                          const std::string &req_model) {
    std::vector<Database::ModelMapping> mappings;
    int template_id = db.get_key_template_id(local_key_id);
    if (template_id > 0) {
        mappings = db.get_template_entries(template_id);
        fprintf(stderr, "[Proxy] Using template %d: %zu mapping(s)\n",
                template_id, mappings.size());
    }
    for (const auto &m : mappings) {
        if (glob_match(m.pattern, req_model)) {
            fprintf(stderr, "[Proxy] Model map: %s → %s (pattern: %s)\n",
                    req_model.c_str(), m.upstream_model.c_str(),
                    m.pattern.c_str());
            return fmt::strip_one_m_suffix_for_upstream(m.upstream_model);
        }
    }
    return fmt::strip_one_m_suffix_for_upstream(req_model);
}

/// Apply model mapping to a raw request body string (passthrough path).
/// Returns the resolved upstream model name; `body` is modified in-place.
static std::string apply_model_mapping(Database &db, int local_key_id,
                                       std::string &body) {
    std::string req_model = extract_model(body);
    std::string mapped = resolve_upstream_model(db, local_key_id, req_model);
    if (mapped == req_model) return req_model;
    try {
        json j = json::parse(body);
        j["model"] = mapped;
        body = j.dump();
    } catch (...) {
        size_t pos = body.find("\"model\"");
        if (pos != std::string::npos) {
            auto colon = body.find(':', pos);
            auto q1 = body.find('"', colon + 1);
            auto q2 = body.find('"', q1 + 1);
            if (q1 != std::string::npos && q2 != std::string::npos)
                body.replace(q1 + 1, q2 - q1 - 1, mapped);
        }
    }
    return mapped;
}

// ── add_cors_headers ─────────────────────────────────────────────────────

void ProxyServer::add_cors_headers(httplib::Response &res) {
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.set_header("Access-Control-Allow-Headers",
                   "Authorization, Content-Type");
}

// ── Format resolution helpers ────────────────────────────────────────────

/// Resolve the harness (client-side) format for a route.  Explicit key config
/// wins (用户填写为准); unset → the account's api_format (passthrough).
static ir::HarnessFormat resolve_harness_format(const Router::RouteResult &route) {
    ir::HarnessFormat hf = ir::parse_harness_format(route.harness_format);
    if (hf != ir::HarnessFormat::Unset) return hf;
    switch (ir::parse_api_format(route.api_format)) {
        case ir::ApiFormat::OpenAIResponses: return ir::HarnessFormat::OpenAIResponses;
        case ir::ApiFormat::Anthropic: return ir::HarnessFormat::Anthropic;
        default: return ir::HarnessFormat::OpenAI;
    }
}

static ir::ApiFormat harness_to_api(ir::HarnessFormat hf) {
    switch (hf) {
        case ir::HarnessFormat::OpenAIResponses: return ir::ApiFormat::OpenAIResponses;
        case ir::HarnessFormat::Anthropic: return ir::ApiFormat::Anthropic;
        default: return ir::ApiFormat::OpenAI;
    }
}

/// Resolved upstream target (path + auth/path options) for a route.
struct UpstreamTarget {
    std::string path;
    ForwardOptions opts;
};
static UpstreamTarget resolve_upstream_target(const Router::RouteResult &route) {
    UpstreamTarget t;
    resolve_upstream_path(route, t.path, t.opts.path_is_full);
    t.opts.auth_scheme = route.auth_header;
    return t;
}

/// Non-streaming usage parser dispatcher by upstream api_format.
static std::optional<UsageTracker::UsageInfo>
parse_usage_for_format(const std::string &api_format, const std::string &body) {
    if (api_format == "anthropic") return UsageTracker::parse_anthropic_usage(body);
    if (api_format == "openai_responses") return UsageTracker::parse_responses_usage(body);
    return UsageTracker::parse_usage(body);
}

static UsageTracker::UsageInfo usage_from_ir(const ir::Usage &u) {
    UsageTracker::UsageInfo info;
    info.prompt_tokens = u.prompt_tokens;
    info.completion_tokens = u.completion_tokens;
    info.total_tokens = u.total_tokens;
    return info;
}

/// Check whether the client disconnected while we waited for upstream.
static bool client_disconnected(const httplib::Request &req, int inflight_id,
                                const std::string &model) {
    if (req.client_socket == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = req.client_socket;
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, 0) > 0 &&
        (pfd.revents & (POLLHUP | POLLERR | POLLRDHUP))) {
        fprintf(stderr, "[Proxy] Client gone, drop response "
                        "(inflight=%d, model=%s)\n", inflight_id, model.c_str());
        return true;
    }
    return false;
}

// ── handle_chat_request ──────────────────────────────────────────────────

/// Entry point for /v1/chat/completions, /v1/messages and /v1/responses.
/// The harness format comes from the local key config; when it matches the
/// account's api_format we use the passthrough fast path, otherwise we
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

    ir::HarnessFormat hf = resolve_harness_format(ar.route);
    ir::ApiFormat upstream = ir::parse_api_format(ar.route.api_format);

    if (harness_to_api(hf) == upstream) {
        handle_passthrough(ar.route, upstream, req, res, t0);
    } else {
        handle_converted(ar.route, harness_to_api(hf), upstream, req, res, t0);
    }
}

// ── handle_passthrough ───────────────────────────────────────────────────

void ProxyServer::handle_passthrough(const Router::RouteResult &route,
                                     ir::ApiFormat upstream,
                                     const httplib::Request &req,
                                     httplib::Response &res,
                                     std::chrono::steady_clock::time_point t0) {
    std::string body = req.body;
    std::string req_model = apply_model_mapping(db_, route.local_key_id, body);

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
            [this, route, target, body, content_type, model_copy, t0,
             concurrent_count, inflight_id, think_filter, upstream_fmt](
                size_t /*offset*/, httplib::DataSink &sink) -> bool {

                std::string accumulated;
                bool first_response = true;
                std::chrono::steady_clock::time_point t_first_resp;
                ThinkStreamFilter filter;
                bool has_reasoning = false;

                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    if (first_response) {
                        t_first_resp = std::chrono::steady_clock::now();
                        first_response = false;
                    }
                    accumulated.append(data, len);
                    if (!think_filter) return sink.write(data, len);
                    if (!has_reasoning && sse_chunk_has_reasoning(data, len))
                        has_reasoning = true;
                    if (has_reasoning) return sink.write(data, len);
                    std::string filtered = filter.feed(data, len);
                    if (!filtered.empty())
                        return sink.write(filtered.data(), filtered.size());
                    return true;
                };

                auto fwd = forward_with_retry(
                    [&]() {
                        accumulated.clear();
                        first_response = true;
                        return upstream_.forward(
                            "POST", route.base_url, route.upstream_key,
                            target.path, body, content_type, on_chunk, target.opts);
                    },
                    /*is_streaming=*/true);

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
                sink.done();
                return true;
            },
            /* user_data */ nullptr);
        return;
    }

    // ── Non-streaming path ──────────────────────────────────────────
    auto fwd = forward_with_retry(
        [&]() {
            return upstream_.forward(
                "POST", route.base_url, route.upstream_key,
                target.path, body, content_type, nullptr, target.opts);
        },
        /*is_streaming=*/false);

    if (client_disconnected(req, inflight_id, req_model)) {
        db_.request_end(inflight_id);
        return;
    }

    res.set_header("X-Retry-Count", std::to_string(fwd.retries));
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
        res.status = 502;
        res.set_content(json_error("Upstream error: " + fwd.error, 502),
                        "application/json");
    }
}

// ── handle_converted ─────────────────────────────────────────────────────

void ProxyServer::handle_converted(const Router::RouteResult &route,
                                   ir::ApiFormat harness, ir::ApiFormat upstream,
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

    cReq.model = resolve_upstream_model(db_, route.local_key_id, cReq.model);
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
            [this, route, target, upstream_body, model_copy, upstream_fmt, t0,
             concurrent_count, inflight_id, harness_codec_ptr, upstream_codec_ptr](
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

                auto fwd = forward_with_retry(
                    [&]() {
                        accumulated.clear();
                        first_response = true;
                        return upstream_.forward(
                            "POST", route.base_url, route.upstream_key,
                            target.path, upstream_body, "application/json",
                            on_chunk, target.opts);
                    },
                    /*is_streaming=*/true);

                if (fwd.status_code >= 400 || !fwd.success) {
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
                        last_usage.total_tokens = fb->total_tokens;
                    }
                }
                auto usage_info = usage_from_ir(last_usage);
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
                sink.done();
                return true;
            },
            /* user_data */ nullptr);
        return;
    }

    // ── Non-streaming path ──────────────────────────────────────────
    auto fwd = forward_with_retry(
        [&]() {
            return upstream_.forward(
                "POST", route.base_url, route.upstream_key,
                target.path, upstream_body, "application/json", nullptr, target.opts);
        },
        /*is_streaming=*/false);

    if (client_disconnected(req, inflight_id, model_copy)) {
        db_.request_end(inflight_id);
        return;
    }

    res.set_header("X-Retry-Count", std::to_string(fwd.retries));
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
            tracker_.log_request(route.account_id, route.local_key_id,
                                 usage_from_ir(cResp.usage), false,
                                 fwd.status_code, fwd.duration_ms);
            res.status = fwd.status_code;
            res.set_content(harness_codec.serialize_response(cResp).dump(),
                            "application/json");
        } else {
            auto fb = parse_usage_for_format(upstream_fmt, fwd.body);
            if (fb.has_value()) {
                tracker_.log_request(route.account_id, route.local_key_id,
                                     *fb, false, fwd.status_code, fwd.duration_ms);
            }
            res.status = fwd.status_code;
            res.set_content(fwd.body, "application/json");
        }
    } else {
        json normalized;
        try {
            normalized = upstream_codec.parse_error_body(json::parse(fwd.body));
        } catch (...) {
            normalized = json{{"message", fwd.error.empty() ? "upstream error" : fwd.error}};
        }
        res.status = (fwd.status_code >= 400) ? fwd.status_code : 502;
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

    auto fwd = forward_with_retry(
        [&]() {
            return upstream_.forward("GET", ar.route.base_url,
                                     ar.route.upstream_key,
                                     "/models", "", "application/json", nullptr);
        },
        /*is_streaming=*/false);

    res.set_header("X-Retry-Count", std::to_string(fwd.retries));
    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    if (fwd.success) {
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        res.status = 502;
        res.set_content(json_error("Upstream error: " + fwd.error, 502),
                        "application/json");
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

    // 2. Apply model mapping
    std::string body = req.body;
    std::string req_model = apply_model_mapping(db_, ar.route.local_key_id, body);

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
    auto fwd = forward_with_retry(
        [&]() {
            return upstream_.forward(
                "POST", ar.route.base_url, ar.route.upstream_key,
                "/embeddings", body, content_type, nullptr);
        },
        /*is_streaming=*/false);

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

    res.set_header("X-Retry-Count", std::to_string(fwd.retries));
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
        res.status = 502;
        res.set_content(
            json_error("Upstream error: " + fwd.error, 502),
            "application/json");
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

    // The three chat endpoints share one format-agnostic pipeline.
    auto chat_handler = [this](const httplib::Request &req, httplib::Response &res) {
        handle_chat_request(req, res);
    };
    server.Post("/v1/chat/completions", chat_handler);
    server.Post("/v1/messages", chat_handler);
    server.Post("/v1/responses", chat_handler);

    // Embedding endpoint
    server.Post("/v1/embeddings",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_embeddings(req, res);
                });

    // Model listing
    server.Get("/v1/models",
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
