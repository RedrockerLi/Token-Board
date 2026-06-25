#include "proxy_server.h"
#include "db.h"
#include "router.h"
#include "think_filter.h"
#include "upstream_client.h"
#include "usage_tracker.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

#include <chrono>
#include <cstdio>
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
/// Case-insensitive. Used for key_model_map pattern matching.
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

// ── Model-mapping helper ─────────────────────────────────────────────────

/// Apply model mapping (template or key_model_map) to the request body.
/// Returns the resolved upstream model name; `body` is modified in-place.
static std::string apply_model_mapping(Database &db,
                                        int local_key_id,
                                        std::string &body) {
    std::string req_model = extract_model(body);
    std::vector<Database::ModelMapping> mappings;
    int template_id = db.get_key_template_id(local_key_id);
    if (template_id > 0) {
        mappings = db.get_template_entries(template_id);
        fprintf(stderr, "[Proxy] Using template %d: %zu mapping(s)\n",
                template_id, mappings.size());
    } else {
        mappings = db.get_key_model_mappings(local_key_id);
        fprintf(stderr, "[Proxy] Got %zu model mappings for key_id=%d\n",
                mappings.size(), local_key_id);
    }
    for (const auto &m : mappings) {
        if (glob_match(m.pattern, req_model)) {
            fprintf(stderr, "[Proxy] Model map: %s → %s (pattern: %s)\n",
                    req_model.c_str(), m.upstream_model.c_str(), m.pattern.c_str());
            try {
                json j = json::parse(body);
                j["model"] = m.upstream_model;
                body = j.dump();
            } catch (...) {
                size_t pos = body.find("\"model\"");
                if (pos != std::string::npos) {
                    auto colon = body.find(':', pos);
                    auto q1 = body.find('"', colon + 1);
                    auto q2 = body.find('"', q1 + 1);
                    if (q1 != std::string::npos && q2 != std::string::npos)
                        body.replace(q1 + 1, q2 - q1 - 1, m.upstream_model);
                }
            }
            return m.upstream_model;
        }
    }
    return req_model;
}

// ── add_cors_headers ─────────────────────────────────────────────────────

void ProxyServer::add_cors_headers(httplib::Response &res) {
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.set_header("Access-Control-Allow-Headers",
                   "Authorization, Content-Type");
}

// ── handle_chat_completions ──────────────────────────────────────────────

void ProxyServer::handle_chat_completions(const httplib::Request &req,
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

    // 1.5. Format enforcement: /v1/chat/completions → OpenAI only
    if (ar.route.api_format != "openai") {
        res.status = 400;
        res.set_content(
            json_error("This account uses the Anthropic API format. "
                       "Use POST /v1/messages instead.", 400),
            "application/json");
        return;
    }

    // 2. Apply model mapping
    std::string body = req.body;
    std::string req_model = apply_model_mapping(db_, ar.route.local_key_id, body);

    // 3. Determine streaming mode & content type
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    bool is_stream = is_streaming_request(body);

    // ── Register in-flight request ──
    int inflight_id = db_.request_start(ar.route.local_key_id,
                                        ar.route.account_id,
                                        req_model, is_stream);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] %s request from key_id=%d to account=%d model=%s "
                    "(concurrent=%d, inflight_id=%d)\n",
            is_stream ? "streaming" : "non-streaming",
            ar.route.local_key_id, ar.route.account_id,
            req_model.c_str(), concurrent_count, inflight_id);

    // Capture model for use in streaming lambda
    auto model_copy = req_model;

    if (is_stream) {
        // ── Streaming path ──────────────────────────────────────────

        res.set_chunked_content_provider(
            "text/event-stream",
            [this, route = ar.route, body, content_type, model_copy, t0,
             concurrent_count, inflight_id](
                size_t /*offset*/, httplib::DataSink &sink) -> bool {

                std::string accumulated;
                bool client_connected = true;
                bool first_response = true;
                std::chrono::steady_clock::time_point t_first_resp;

                // ── Think-tag filter for streaming ──
                ThinkStreamFilter filter;
                bool has_reasoning = false;

                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    if (first_response) {
                        t_first_resp = std::chrono::steady_clock::now();
                        first_response = false;
                    }
                    accumulated.append(data, len);

                    if (!has_reasoning &&
                        sse_chunk_has_reasoning(data, len))
                        has_reasoning = true;

                    if (has_reasoning)
                        return sink.write(data, len);

                    std::string filtered = filter.feed(data, len);
                    if (!filtered.empty())
                        return sink.write(filtered.data(), filtered.size());
                    return true;  // chunk suppressed (inside think block)
                };

                auto fwd = forward_with_retry(
                    [&]() {
                        accumulated.clear();
                        first_response = true;
                        return upstream_.forward(
                            "POST", route.base_url, route.upstream_key,
                            "/chat/completions", body, content_type, on_chunk);
                    },
                    /*is_streaming=*/true);

                // Parse usage from accumulated SSE data
                auto usage = UsageTracker::parse_usage_from_sse(fwd.body);
                if (usage.has_value()) {
                    tracker_.log_request(route.account_id,
                                         route.local_key_id,
                                         *usage, true, fwd.status_code,
                                         fwd.duration_ms);
                } else {
                    fprintf(stderr, "[Proxy] Warning: could not parse usage "
                                    "from streaming response\n");
                }

                tracker_.mark_key_used(route.local_key_id);

                // ── Perf: proxy TTFT + concurrent snapshot ──
                int proxy_ttft = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_response ? std::chrono::steady_clock::now() - t0 : t_first_resp - t0).count());
                tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                                        fwd.status_code, fwd.status_code >= 400,
                                        concurrent_count);

                // ── Mark request as completed ──
                db_.request_end(inflight_id);

                sink.done();
                return true;
            },
            /* user_data */ nullptr);

    } else {
        // ── Non-streaming path ──────────────────────────────────────

        auto fwd = forward_with_retry(
            [&]() {
                return upstream_.forward(
                    "POST", ar.route.base_url, ar.route.upstream_key,
                    "/chat/completions", body, content_type, nullptr);
            },
            /*is_streaming=*/false);

        // ── Retry / upstream metadata headers ──
        res.set_header("X-Retry-Count", std::to_string(fwd.retries));
        res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

        // Parse usage from JSON response
        auto usage = UsageTracker::parse_usage(fwd.body);
        if (usage.has_value()) {
            tracker_.log_request(ar.route.account_id,
                                 ar.route.local_key_id,
                                 *usage, false, fwd.status_code,
                                 fwd.duration_ms);
        } else {
            fprintf(stderr, "[Proxy] Warning: could not parse usage "
                            "from non-streaming response, model=%s\n",
                            req_model.c_str());
        }

        tracker_.mark_key_used(ar.route.local_key_id);

        // ── Perf: proxy TTFT (non-streaming = total time) ──
        auto t1 = std::chrono::steady_clock::now();
        int proxy_ttft = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        tracker_.log_perf_event(req_model, fwd.ttft_ms, proxy_ttft,
                                fwd.status_code, fwd.status_code >= 400,
                                concurrent_count);

        // ── Mark request as completed ──
        db_.request_end(inflight_id);

        if (fwd.success) {
            // Sanitize: strip <think> tags from response
            std::string clean_body = sanitize_response_body(fwd.body);
            res.status = fwd.status_code;
            res.set_content(clean_body, "application/json");
        } else {
            res.status = 502;
            res.set_content(
                json_error("Upstream error: " + fwd.error, 502),
                "application/json");
        }
    }
}

// ── handle_anthropic_messages ─────────────────────────────────────────────

void ProxyServer::handle_anthropic_messages(const httplib::Request &req,
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

    // 1.5. Format enforcement: /v1/messages → Anthropic only
    if (ar.route.api_format != "anthropic") {
        res.status = 400;
        res.set_content(
            json_error("This account uses the OpenAI API format. "
                       "Use POST /v1/chat/completions instead.", 400),
            "application/json");
        return;
    }

    // 2. Apply model mapping (same as OpenAI handler)
    std::string body = req.body;
    std::string req_model = apply_model_mapping(db_, ar.route.local_key_id, body);

    // 3. Determine streaming mode & content type
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    bool is_stream = is_streaming_request(body);

    // ── Register in-flight request ──
    int inflight_id = db_.request_start(ar.route.local_key_id,
                                        ar.route.account_id,
                                        req_model, is_stream);
    int concurrent_count = db_.get_in_flight_count();

    fprintf(stderr, "[Proxy] Anthropic %s request from key_id=%d to account=%d model=%s "
                    "(concurrent=%d, inflight_id=%d)\n",
            is_stream ? "streaming" : "non-streaming",
            ar.route.local_key_id, ar.route.account_id,
            req_model.c_str(), concurrent_count, inflight_id);

    auto model_copy = req_model;

    // Anthropic upstream path is /v1/messages regardless of base_url path
    const std::string anthropic_path = "/v1/messages";

    if (is_stream) {
        // ── Streaming path ──────────────────────────────────────────

        res.set_chunked_content_provider(
            "text/event-stream",
            [this, route = ar.route, body, content_type, model_copy, t0,
             concurrent_count, inflight_id, anthropic_path](
                size_t /*offset*/, httplib::DataSink &sink) -> bool {

                std::string accumulated;
                bool client_connected = true;
                bool first_response = true;
                std::chrono::steady_clock::time_point t_first_resp;

                // No think-tag filtering for Anthropic format
                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    if (first_response) {
                        t_first_resp = std::chrono::steady_clock::now();
                        first_response = false;
                    }
                    accumulated.append(data, len);
                    return sink.write(data, len);
                };

                auto fwd = forward_with_retry(
                    [&]() {
                        accumulated.clear();
                        first_response = true;
                        return upstream_.forward(
                            "POST", route.base_url, route.upstream_key,
                            anthropic_path, body, content_type, on_chunk);
                    },
                    /*is_streaming=*/true);

                // Parse usage from accumulated SSE data (Anthropic format)
                auto usage = UsageTracker::parse_anthropic_usage_from_sse(fwd.body);
                if (usage.has_value()) {
                    tracker_.log_request(route.account_id,
                                         route.local_key_id,
                                         *usage, true, fwd.status_code,
                                         fwd.duration_ms);
                } else {
                    fprintf(stderr, "[Proxy] Warning: could not parse Anthropic usage "
                                    "from streaming response\n");
                }

                tracker_.mark_key_used(route.local_key_id);

                // ── Perf: proxy TTFT + concurrent snapshot ──
                int proxy_ttft = static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_response ? std::chrono::steady_clock::now() - t0 : t_first_resp - t0).count());
                tracker_.log_perf_event(model_copy, fwd.ttft_ms, proxy_ttft,
                                        fwd.status_code, fwd.status_code >= 400,
                                        concurrent_count);

                // ── Mark request as completed ──
                db_.request_end(inflight_id);

                sink.done();
                return true;
            },
            /* user_data */ nullptr);

    } else {
        // ── Non-streaming path ──────────────────────────────────────

        auto fwd = forward_with_retry(
            [&]() {
                return upstream_.forward(
                    "POST", ar.route.base_url, ar.route.upstream_key,
                    anthropic_path, body, content_type, nullptr);
            },
            /*is_streaming=*/false);

        // ── Retry / upstream metadata headers ──
        res.set_header("X-Retry-Count", std::to_string(fwd.retries));
        res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

        // Parse usage from JSON response (Anthropic format)
        auto usage = UsageTracker::parse_anthropic_usage(fwd.body);
        if (usage.has_value()) {
            tracker_.log_request(ar.route.account_id,
                                 ar.route.local_key_id,
                                 *usage, false, fwd.status_code,
                                 fwd.duration_ms);
        } else {
            fprintf(stderr, "[Proxy] Warning: could not parse Anthropic usage "
                            "from non-streaming response, model=%s\n",
                            req_model.c_str());
        }

        tracker_.mark_key_used(ar.route.local_key_id);

        // ── Perf: proxy TTFT (non-streaming = total time) ──
        auto t1 = std::chrono::steady_clock::now();
        int proxy_ttft = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        tracker_.log_perf_event(req_model, fwd.ttft_ms, proxy_ttft,
                                fwd.status_code, fwd.status_code >= 400,
                                concurrent_count);

        // ── Mark request as completed ──
        db_.request_end(inflight_id);

        if (fwd.success) {
            // No sanitize_response_body for Anthropic (different structure)
            res.status = fwd.status_code;
            res.set_content(fwd.body, "application/json");
        } else {
            res.status = 502;
            res.set_content(
                json_error("Upstream error: " + fwd.error, 502),
                "application/json");
        }
    }
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

    // OpenAI-compatible proxy endpoint
    server.Post("/v1/chat/completions",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_chat_completions(req, res);
                });

    // Anthropic-compatible proxy endpoint
    server.Post("/v1/messages",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_anthropic_messages(req, res);
                });

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
