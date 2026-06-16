#include "proxy_server.h"
#include "db.h"
#include "router.h"
#include "upstream_client.h"
#include "usage_tracker.h"
#include "model_pricing.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

#include <cstdio>

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

    // 1. Extract Bearer token
    std::string auth = req.has_header("Authorization")
                           ? req.get_header_value("Authorization")
                           : "";

    std::string local_key;
    if (auth.rfind("Bearer ", 0) == 0) {
        local_key = auth.substr(7);
    }

    if (local_key.empty()) {
        res.status = 401;
        res.set_content(json_error("Missing API key. Use: Authorization: Bearer <key>", 401),
                        "application/json");
        return;
    }

    // 2. Route — look up local key → upstream account
    auto route_result = router_.route(local_key);
    if (!route_result.success) {
        res.status = 401;
        res.set_content(json_error(route_result.error, 401),
                        "application/json");
        return;
    }

    // 3. Apply model mapping (template first, then key_model_map fallback)
    std::string body = req.body;
    std::string req_model = extract_model(body);
    std::vector<Database::ModelMapping> mappings;
    int template_id = db_.get_key_template_id(route_result.local_key_id);
    if (template_id > 0) {
        mappings = db_.get_template_entries(template_id);
        fprintf(stderr, "[Proxy] Using template %d: %zu mapping(s)\n", template_id, mappings.size());
    } else {
        mappings = db_.get_key_model_mappings(route_result.local_key_id);
        fprintf(stderr, "[Proxy] Got %zu model mappings for key_id=%d\n",
                mappings.size(), route_result.local_key_id);
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
            req_model = m.upstream_model;
            break;
        }
    }

    // 4. Forward to upstream
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    bool is_stream = is_streaming_request(body);

    fprintf(stderr, "[Proxy] %s request from key_id=%d to account=%d model=%s\n",
            is_stream ? "streaming" : "non-streaming",
            route_result.local_key_id, route_result.account_id,
            req_model.c_str());

    // Capture model for use in streaming lambda
    auto model_copy = req_model;

    if (is_stream) {
        // ── Streaming path ──────────────────────────────────────────

        res.set_chunked_content_provider(
            "text/event-stream",
            [this, route_result, body, content_type, model_copy](
                size_t /*offset*/, httplib::DataSink &sink) -> bool {

                std::string accumulated;
                bool client_connected = true;

                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    accumulated.append(data, len);
                    return sink.write(data, len);
                };

                // Retry up to 3 times
                UpstreamClient::ForwardResult fwd;
                for (int attempt = 0; attempt < 3; attempt++) {
                    if (attempt > 0) {
                        fprintf(stderr, "[Proxy] Retry %d/3 for streaming request\n", attempt + 1);
                        accumulated.clear();
                    }
                    fwd = upstream_.forward(
                        route_result.base_url, route_result.upstream_key,
                        "/chat/completions", body, content_type, on_chunk);
                    if (fwd.success) break;
                }

                // Parse usage from accumulated SSE data
                auto usage = UsageTracker::parse_usage_from_sse(fwd.body);
                if (usage.has_value()) {
                    tracker_.log_request(route_result.account_id,
                                         route_result.local_key_id,
                                         *usage, true, fwd.status_code,
                                         fwd.duration_ms);
                } else {
                    fprintf(stderr, "[Proxy] Warning: could not parse usage "
                                    "from streaming response\n");
                    // Don't write empty records — they pollute the dashboard with "unknown" models
                }

                tracker_.mark_key_used(route_result.local_key_id);
                sink.done();
                return true;
            },
            /* user_data */ nullptr);

    } else {
        // ── Non-streaming path ──────────────────────────────────────

        UpstreamClient::ForwardResult fwd;
        for (int attempt = 0; attempt < 3; attempt++) {
            if (attempt > 0)
                fprintf(stderr, "[Proxy] Retry %d/3 for non-streaming request\n", attempt + 1);
            fwd = upstream_.forward(
                route_result.base_url, route_result.upstream_key,
                "/chat/completions", body, content_type, nullptr);
            if (fwd.success) break;
        }

        // Parse usage from JSON response
        auto usage = UsageTracker::parse_usage(fwd.body);
        if (usage.has_value()) {
            tracker_.log_request(route_result.account_id,
                                 route_result.local_key_id,
                                 *usage, false, fwd.status_code,
                                 fwd.duration_ms);
        } else {
            fprintf(stderr, "[Proxy] Warning: could not parse usage "
                            "from non-streaming response, model=%s\n",
                            req_model.c_str());
        }

        tracker_.mark_key_used(route_result.local_key_id);

        if (fwd.success) {
            // Forward the response as-is
            // Parse upstream response to preserve Content-Type
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

    std::string auth = req.has_header("Authorization")
                           ? req.get_header_value("Authorization")
                           : "";

    std::string local_key;
    if (auth.rfind("Bearer ", 0) == 0)
        local_key = auth.substr(7);

    if (local_key.empty()) {
        res.status = 401;
        res.set_content(json_error("Missing API key", 401), "application/json");
        return;
    }

    auto route_result = router_.route(local_key);
    if (!route_result.success) {
        res.status = 401;
        res.set_content(json_error(route_result.error, 401), "application/json");
        return;
    }

    auto fwd = upstream_.forward(route_result.base_url, route_result.upstream_key,
                                 "/models", "", "application/json", nullptr);
    if (fwd.success) {
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        res.status = 502;
        res.set_content(json_error("Upstream error: " + fwd.error, 502), "application/json");
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
    server.Options("/v1/models", cors_handler);

    // Main proxy endpoint
    server.Post("/v1/chat/completions",
                [this](const httplib::Request &req, httplib::Response &res) {
                    handle_chat_completions(req, res);
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
