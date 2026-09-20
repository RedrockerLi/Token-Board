#include "proxy_server_internal.h"

void ProxyServer::handle_embeddings(const httplib::Request &req,
                                    httplib::Response &res) {
    EndpointRunner endpoint_runner(*this);
    add_cors_headers(res);
    const auto &policy = endpoint_policy(EndpointKind::Embeddings);

    auto t0 = std::chrono::steady_clock::now();
    const auto request_id = allocate_request_id();
    auto log_context = make_proxy_log_context(
        request_id, req.method, req.path, ir::to_string(policy.client_format));

    // 1. Auth + route lookup
    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        log_proxy_auth_failure(log_context, req.has_header("Authorization"),
                               req.has_header("x-api-key"));
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    // 2. Resolve effective upstream targets (strip [1m] marker; aggregate
    //    routing yields multiple candidates in priority order).
    RequestContext context;
    std::string parse_error;
    if (!parse_request_context(req, context, parse_error)) {
        log_proxy_failure(log_context, "request_validation", "invalid_request", 400);
        res.status = 400;
        res.set_content(json_error(parse_error, 400),
                        "application/json");
        return;
    }
    std::string req_model = context.model;
    log_context.model = req_model;
    log_context.route_account_id = ar.route.account_id;
    log_context.local_key_id = ar.route.local_key_id;
    auto cands = resolve_candidates_cached(ar.route, req_model);
    if (cands.empty()) {
        log_proxy_failure(log_context, "routing", "model_unavailable", 400);
        res.status = 400;
        res.set_content(json_error("Model '" + req_model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }
    if (!endpoint_runner.try_reserve_accounting()) {
        log_proxy_failure(log_context, "accounting", "accounting_unavailable", 503);
        res.status = 503;
        res.set_header("Retry-After", "1");
        res.set_content(json_error(
            "Accounting writer is temporarily unavailable", 503),
            "application/json");
        return;
    }
    // Committed to contacting an upstream: an abnormal exit below writes an
    // internal_abort UsageEvent instead of dropping the reserved slot.
    endpoint_runner.mark_accounting_upstream_started(
        ar.route.account_id, ar.route.local_key_id, req_model, false);
    RequestBodyCache body_cache(req.body, context.parsed_json, context.model);

    // 3. Determine content type
    std::string content_type = req.has_header("Content-Type")
                                   ? req.get_header_value("Content-Type")
                                   : "application/json";

    // Embeddings are always non-streaming: try candidates in order, falling
    // back on 429/5xx (nothing has been sent to the client yet).
    int concurrent_count = 0;
    const auto embedding_timeouts = timeout_config_cached(EndpointKind::Embeddings);
    const int embedding_budget_seconds = embedding_timeouts.non_streaming_timeout > 0
        ? embedding_timeouts.non_streaming_timeout : 600;
    const auto embedding_deadline = t0 + std::chrono::seconds(embedding_budget_seconds);

    std::vector<std::size_t> order(cands.size());
    std::iota(order.begin(), order.end(), 0);
    AttemptExecutor executor(gate_);
    auto outcome = executor.execute(
        {&cands, order, embedding_deadline, embedding_budget_seconds,
         [this, request_id](const std::string &m) {
             return request_started(request_id, m, false);
         },
         [this](std::uint64_t id) { request_finished(id); },
         [&](const AttemptRequest &attempt) {
        const auto &c = attempt.candidate;
        concurrent_count = in_flight_count();

        TB_LOG_DEBUG("[Proxy] embedding request from key_id=%d to account=%d "
                        "model=%s (concurrent=%d)\n",
                ar.route.local_key_id, c.account().id,
                c.upstream_model().c_str(), concurrent_count);

        return forward_endpoint_attempt(
            upstream_, policy, c, body_cache.for_candidate(c), content_type,
            static_cast<int>(attempt.remaining_budget_ms), embedding_timeouts,
            req.client_socket);
        },
        [&](const UpstreamClient::ForwardResult &result) {
            return result.client_disconnected ||
                   client_socket_gone(req.client_socket);
        },
        proxy_attempt_failure_logger(log_context),
        proxy_candidate_skip_logger(log_context)});
    auto &fwd = outcome.result;
    const auto *used = outcome.used;
    const auto *last_attempted = outcome.last_attempted;
    const auto &attempts = outcome.attempts;

    if (!used) {
        const int final_status = no_upstream_status(
            fwd, attempts, outcome.no_candidate_reason);
        const char *reason = proxy_terminal_failure_reason(outcome, fwd);
        log_proxy_failure(log_context, "request_final", reason, final_status,
                          fwd.duration_ms, attempts.size(), attempts.size(),
                          false, fwd.timeout_secs, last_attempted);
        enqueue_zero_usage(last_attempted ? last_attempted->account().id
                                          : ar.route.account_id,
                           ar.route.local_key_id, req_model, false, final_status,
                           static_cast<int>(std::chrono::duration_cast<
                               std::chrono::milliseconds>(
                               std::chrono::steady_clock::now() - t0).count()),
                           last_attempted ? last_attempted->key_slot_id : 0,
                           static_cast<int>(attempts.size()), attempts);
        TerminalErrorOptions error_options;
        error_options.no_candidate_reason = outcome.no_candidate_reason;
        const auto err = render_terminal_error(
            codecs_.get(policy.client_format), &codecs_.get(policy.client_format),
            fwd, attempts, error_options);
        res.status = err.status;
        if (err.close_connection) res.set_header("Connection", "close");
        if (err.retry_after_seconds > 0)
            res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
        res.set_content(err.body, "application/json");
        return;
    }

    // ── Check if client disconnected while waiting for upstream ──
    if (fwd.client_disconnected || client_socket_gone(req.client_socket)) {
            log_proxy_client_disconnect(log_context, fwd.duration_ms,
                                        attempts.size(), used);
            enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                               req_model, false, 499, fwd.duration_ms,
                               used->key_slot_id,
                               static_cast<int>(attempts.size()), attempts);
            return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    // Parse usage
    const bool response_json_valid = fwd.success && proxy_response_json_valid(
        log_context, fwd.body, fwd.status_code, fwd.duration_ms,
        attempts.size(), used);
    auto usage = parse_usage_for_format(used->account().api_format, fwd.body);
    if (usage.has_value()) {
        // Prefer the model reported by the successful upstream.  A provider
        // may omit it, so fall back to the model actually sent to that
        // candidate (including route rewrites).
        usage->model = fwd.success
            ? model_for_success_log(usage->model, *used)
            : req_model;
        enqueue_log(used->account().id,
                             ar.route.local_key_id,
                             *usage, false, fwd.status_code,
                             fwd.duration_ms, used->key_slot_id,
                             -1, -1, -1.0, -1, -1,
                             static_cast<int>(attempts.size()), attempts);
    } else {
        if (fwd.success && response_json_valid)
            log_proxy_usage_unavailable(log_context, fwd.status_code,
                                        fwd.duration_ms, attempts.size(), used);
        enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                           fwd.success ? model_for_success_log("", *used)
                                       : req_model,
                           false, fwd.status_code, fwd.duration_ms,
                           used->key_slot_id,
                           static_cast<int>(attempts.size()), attempts);
    }

    if (fwd.success) {
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        log_proxy_failure(log_context, "request_final",
                          proxy_failure_reason(fwd), fwd.status_code,
                          fwd.duration_ms, attempts.size(), attempts.size(),
                          false, fwd.timeout_secs, used);
        const auto err = render_terminal_error(
            codecs_.get(policy.client_format), &codecs_.get(policy.client_format),
            fwd, attempts, {.used = true});
        res.status = err.status;
        if (err.close_connection) res.set_header("Connection", "close");
        if (err.retry_after_seconds > 0)
            res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
        res.set_content(err.body, "application/json");
    }
}

// ── setup_routes ─────────────────────────────────────────────────────────
