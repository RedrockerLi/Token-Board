#include "proxy_server_internal.h"

void ProxyServer::handle_chat_request(const httplib::Request &req,
                                      httplib::Response &res) {
    auto accounting_cleanup = make_scope_exit(
        [this] { release_unconsumed_accounting(); });
    add_cors_headers(res);
    auto t0 = std::chrono::steady_clock::now();

    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    RequestContext context;
    std::string parse_error;
    if (!parse_request_context(req, context, parse_error)) {
        const auto format = harness_format_from_path(req.path);
        res.status = 400;
        res.set_content(codecs_.get(format).serialize_error_body(
            json{{"message", parse_error}, {"type", "parse_error"}}).dump(),
            "application/json");
        return;
    }

    // Aggregate accounts need the request model to pick the real upstream
    // account, so resolution happens here — before the passthrough/converted
    // split.  For plain accounts this only strips the `[1m]`/`[1M]` marker.
    std::string model = context.model;
    auto cands = resolve_candidates_cached(ar.route, model);
    if (cands.empty()) {
        res.status = 400;
        res.set_content(json_error("Model '" + model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }
    const ir::ApiFormat harness = context.client_format;
    const bool conversion_needed = std::any_of(
        cands.begin(), cands.end(), [harness](const UpstreamCandidate &candidate) {
            return ir::parse_api_format(candidate.account().api_format) != harness;
        });
    RequestBodyCache body_cache(req.body, context.parsed_json, context.model,
                                ir::to_string(harness));

    // ── Streaming: defer candidate selection into the provider ─────────
    // The chunked response headers are intentionally not committed until the
    // provider writes its first event.  This lets handle_streaming try the
    // next key when an upstream returns 429/5xx before emitting any bytes.
    if (context.streaming) {
        std::shared_ptr<const ir::ChatRequest> parsed_request;
        if (conversion_needed &&
            !ensure_request_ir(codecs_, context, parse_error)) {
            res.status = 400;
            res.set_content(codecs_.get(harness).serialize_error_body(
                json{{"message", parse_error},
                     {"type", "parse_error"}}).dump(),
                "application/json");
            return;
        }
        if (conversion_needed)
            parsed_request = std::make_shared<const ir::ChatRequest>(
                context.parsed_ir);
        auto candidate_bodies = candidate_request_bodies(
            context.raw_body, context.parsed_json, context.model, cands, harness);
        std::shared_ptr<const json> parsed_json;
        for (const auto &candidate : cands) {
            if (ir::parse_api_format(candidate.account().api_format) == harness &&
                candidate.upstream_model() != context.model) {
                parsed_json = std::make_shared<const json>(context.parsed_json);
                break;
            }
        }
        // Reserve only after all request validation and lazy conversion work
        // has succeeded.  A malformed conversion must not strand accounting
        // capacity for a request that never contacts an upstream.
        if (!try_reserve_accounting()) {
            res.status = 503;
            res.set_header("Retry-After", "1");
            res.set_content(json_error(
                "Accounting writer is temporarily unavailable", 503),
                "application/json");
            return;
        }
        // cpp-httplib invokes the deferred provider after this handler
        // returns. Transfer the reserved slot to that provider; otherwise the
        // handler's scope guard would release capacity before the stream can
        // enqueue its final UsageEvent.
        auto reservation = detach_accounting_reservation();
        const std::string &session_id = context.session_id;
        const auto scope = affinity_scope(ar.route.local_key_id, harness);
        size_t start = affinity_start(affinity_, scope, session_id, cands);
        handle_streaming(cands, candidate_bodies, start, session_id,
                         ar.route.local_key_id, harness, model,
                         std::move(parsed_json), std::move(parsed_request),
                         reservation, req, res, t0);
        return;
    }

    // ── Non-streaming: candidate loop with fallback ────────────────────
    const std::string &content_type = context.content_type;
    const FormatCodec &harness_codec = codecs_.get(harness);
    if (!try_reserve_accounting()) {
        res.status = 503;
        res.set_header("Retry-After", "1");
        res.set_content(json_error(
            "Accounting writer is temporarily unavailable", 503),
            "application/json");
        return;
    }
    // From here the request is committed to contacting an upstream: an abnormal
    // exit below writes an internal_abort UsageEvent instead of dropping the slot.
    mark_accounting_upstream_started(ar.route.account_id, ar.route.local_key_id,
                                     model, false);
    ir::ChatRequest cReq;
    std::string perr;

    int concurrent_count = 0;
    bool think_filter = false;
    ir::ApiFormat used_upstream_fmt = harness;
    const FormatCodec *upstream_codec = nullptr;
    std::optional<ir::ChatResponse> converted_response;

    // Session-affinity spillover: start at the session's preferred candidate
    // and wrap around in fixed order (P, P+1, …, n-1, 0, …, P-1).
    const std::string &session_id = context.session_id;
    const auto scope = affinity_scope(ar.route.local_key_id, harness);
    size_t start = affinity_start(affinity_, scope, session_id, cands);
    const auto order = candidate_order(
        cands, start, routing_rr_.fetch_add(1, std::memory_order_relaxed));
    const auto base_timeouts = timeout_config_cached(
        chat_endpoint_policy(harness).kind);
    const int budget_seconds = base_timeouts.non_streaming_timeout > 0
        ? base_timeouts.non_streaming_timeout : 600;
    const auto deadline = t0 + std::chrono::seconds(budget_seconds);

    AttemptExecutor executor(gate_);
    auto outcome = executor.execute(
        {&cands, order, deadline, budget_seconds,
         [this](const std::string &m) { return request_started(m, false); },
         [this](std::uint64_t id) { request_finished(id); },
         [&](const AttemptRequest &attempt) {
        const auto &c = attempt.candidate;
        concurrent_count = in_flight_count();

        ir::ApiFormat upstream = ir::parse_api_format(c.account().api_format);
        auto attempt_timeouts = base_timeouts;
        attempt_timeouts.non_streaming_timeout = std::max(1, std::min(
            attempt_timeouts.non_streaming_timeout,
            static_cast<int>((attempt.remaining_budget_ms + 999) / 1000)));
        TB_LOG_DEBUG("[Proxy] %s %s request from key_id=%d to account=%d "
                        "credential=%d model=%s (concurrent=%d)\n",
                ir::to_string(harness).c_str(),
                (harness == upstream) ? "passthrough" : "convert",
                ar.route.local_key_id, c.account().id, c.key_slot_id,
                c.upstream_model().c_str(), concurrent_count);

        UpstreamClient::ForwardResult result;
        if (harness == upstream) {
            result = forward_endpoint_attempt(
                upstream_, chat_endpoint_policy(harness), c,
                body_cache.for_candidate(c), content_type,
                attempt.remaining_budget_ms, attempt_timeouts,
                req.client_socket);
            think_filter = (upstream == ir::ApiFormat::OpenAI);
            converted_response.reset();
        } else {
            if (!ensure_request_ir(codecs_, context, perr)) {
                result.status_code = 400;
                result.error = perr.empty() ? "request conversion failed" : perr;
                return result;
            }
            cReq = context.parsed_ir;
            cReq.model = c.upstream_model();
            upstream_codec = &codecs_.get(upstream);
            const std::string &body = body_cache.for_transformed(
                ir::to_string(upstream), c.upstream_model(), [&] {
                    return upstream_codec->serialize_request(cReq).dump();
                });
            result = forward_endpoint_attempt(
                upstream_, chat_endpoint_policy(harness), c, body,
                "application/json", attempt.remaining_budget_ms,
                attempt_timeouts, req.client_socket);
            think_filter = false;
            converted_response.reset();
        }
        used_upstream_fmt = upstream;

        // A 2xx body in the wrong protocol is a failed candidate, not a
        // successful response that can be forwarded verbatim to the harness.
        if (harness != upstream && result.success &&
            result.status_code >= 200 && result.status_code < 300) {
            ir::ChatResponse parsed;
            bool parsed_ok = false;
            perr.clear();
            try {
                parsed_ok = upstream_codec->parse_response(json::parse(result.body),
                                                            parsed, perr);
            } catch (const std::exception &e) {
                perr = e.what();
            } catch (...) {
                perr = "unknown response conversion error";
            }
            if (parsed_ok) {
                converted_response = std::move(parsed);
            } else {
                result.success = false;
                result.status_code = 502;
                result.error = "Invalid upstream response for configured format";
                if (!perr.empty()) result.error += ": " + perr;
            }
        }
        return result;
        },
        [&](const UpstreamClient::ForwardResult &result) {
            return result.client_disconnected ||
                   client_socket_gone(req.client_socket);
        }});
    auto &fwd = outcome.result;
    const auto *used = outcome.used;
    const auto *last_attempted = outcome.last_attempted;
    const auto &attempts = outcome.attempts;
    const int attempts_made = static_cast<int>(attempts.size());

    // Bind only on a successful response below. Failed keys must never become
    // the session's next preferred route.

    if (!used) {
        const int final_status = no_upstream_status(fwd, attempts);
        enqueue_zero_usage(last_attempted ? last_attempted->account().id
                                          : ar.route.account_id,
                           ar.route.local_key_id, model, false, final_status,
                           static_cast<int>(std::chrono::duration_cast<
                               std::chrono::milliseconds>(
                               std::chrono::steady_clock::now() - t0).count()),
                           last_attempted ? last_attempted->key_slot_id : 0,
                           static_cast<int>(attempts.size()), attempts);
        // One shared renderer decides the terminal status (no_upstream_status),
        // normalizes the upstream error (parse_error_body) and applies the
        // harness envelope (serialize_error_body).
        const auto err = render_terminal_error(
            harness_codec, &codecs_.get(used_upstream_fmt), fwd, attempts);
        res.status = err.status;
        if (err.close_connection) res.set_header("Connection", "close");
        res.set_content(err.body, "application/json");
        return;
    }

    if (fwd.client_disconnected || client_disconnected(req, 0, model)) {
        // Record the aborted request truthfully (client closed before we
        // could send a response): status 499, zero tokens.
        int dur = static_cast<int>(std::chrono::duration_cast<
            std::chrono::milliseconds>(std::chrono::steady_clock::now() - t0)
                .count());
        enqueue_zero_usage(used->account().id, ar.route.local_key_id, model,
                           false, 499, dur, used->key_slot_id,
                           static_cast<int>(attempts.size()), attempts);
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    // ── Non-streaming response handling (passthrough vs converted) ──
    if (used_upstream_fmt == harness) {
        if (fwd.success) {
            auto usage = parse_usage_for_format(ir::to_string(used_upstream_fmt),
                                                fwd.body);
            if (usage.has_value()) {
                // Log the client-requested model, not the upstream-echoed one —
                // consistent with converted, streaming and zero-usage paths.
                usage->model = model;
                enqueue_log(used->account().id, ar.route.local_key_id,
                                     *usage, false, fwd.status_code,
                                     fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
            } else {
                TB_LOG_WARN("[Proxy] Warning: could not parse usage "
                                "from non-streaming response, model=%s\n",
                        model.c_str());
                enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                                   model, false, fwd.status_code,
                                   fwd.duration_ms, used->key_slot_id,
                                   attempts_made, attempts);
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
            enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                               model, false, fwd.status_code, fwd.duration_ms,
                               used->key_slot_id, attempts_made, attempts);
            // Passthrough means the upstream body already uses the client's
            // protocol, so a non-empty body is preserved verbatim.
            const auto err = render_terminal_error(
                harness_codec, &codecs_.get(used_upstream_fmt), fwd, attempts,
                {.used = true, .passthrough = true});
            res.status = err.status;
            if (err.close_connection) res.set_header("Connection", "close");
            res.set_content(err.body, "application/json");
        }
    } else {
        if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
            if (converted_response.has_value()) {
                const auto &cResp = *converted_response;
                auto usage_info = usage_from_ir(cResp.usage, used_upstream_fmt);
                usage_info.model = model;
                enqueue_log(used->account().id, ar.route.local_key_id,
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
                enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                                   model, false, 502, fwd.duration_ms,
                                   used->key_slot_id, attempts_made, attempts);
                res.status = 502;
                res.set_content(harness_codec.serialize_error_body(
                    json{{"message", "Invalid converted upstream response"},
                         {"type", "upstream_error"}}).dump(),
                    "application/json");
            }
        } else {
            // Non-2xx / upstream failure: record the failed attempt (zero
            // tokens, truthful status — 504 on timeout, else 502/upstream).
            enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                               model, false, fwd.status_code, fwd.duration_ms,
                               used->key_slot_id, attempts_made, attempts);
            // Converted failures keep a truthful upstream status when >= 400
            // and coerce a sub-400 failure to 502.
            const auto err = render_terminal_error(
                harness_codec,
                upstream_codec ? upstream_codec : &codecs_.get(used_upstream_fmt),
                fwd, attempts, {.used = true, .used_failure_status = 502});
            res.status = err.status;
            if (err.close_connection) res.set_header("Connection", "close");
            res.set_content(err.body, "application/json");
        }
    }

    if (fwd.success && fwd.status_code >= 200 && fwd.status_code < 300) {
        affinity_.bind(scope, session_id, used->key_slot_id);
        if (harness == ir::ApiFormat::OpenAIResponses)
            affinity_.bind(scope, response_id_from_body(fwd.body), used->key_slot_id);
    }
}

// ── handle_streaming ──────────────────────────────────────────────────
