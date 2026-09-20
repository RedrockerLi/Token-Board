#include "proxy_server_internal.h"

namespace {
constexpr const char *kContextManagementBeta =
    "context-management-2025-06-27";

bool has_anthropic_beta(const std::string &header, const std::string &token) {
    size_t begin = 0;
    while (begin <= header.size()) {
        size_t end = header.find(',', begin);
        if (end == std::string::npos) end = header.size();
        size_t first = begin;
        while (first < end && std::isspace(static_cast<unsigned char>(header[first])))
            ++first;
        size_t last = end;
        while (last > first && std::isspace(static_cast<unsigned char>(header[last - 1])))
            --last;
        if (header.compare(first, last - first, token) == 0) return true;
        if (end == header.size()) break;
        begin = end + 1;
    }
    return false;
}

std::string prepare_anthropic_beta(const httplib::Request &request,
                                   const RequestContext &context) {
    std::string header = request.get_header_value("anthropic-beta");
    if (context.client_format != ir::ApiFormat::Anthropic ||
        !context.parsed_json.contains("context_management") ||
        has_anthropic_beta(header, kContextManagementBeta))
        return header;
    if (!header.empty()) header += ",";
    header += kContextManagementBeta;
    return header;
}
}

void ProxyServer::handle_chat_request(const httplib::Request &req,
                                      httplib::Response &res) {
    tb_http_debug::downstream_request(req);
    EndpointRunner endpoint_runner(*this);
    add_cors_headers(res);
    auto t0 = std::chrono::steady_clock::now();
    const auto request_id = allocate_request_id();
    auto log_context = make_proxy_log_context(request_id, req.method, req.path, ir::to_string(harness_format_from_path(req.path)));

    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        log_proxy_auth_failure(log_context, req.has_header("Authorization"), req.has_header("x-api-key"));
        res.status = 401;
        res.set_content(ar.error_json, "application/json");
        return;
    }

    RequestContext context;
    std::string parse_error;
    if (!parse_request_context(req, context, parse_error)) {
        log_proxy_failure(log_context, "request_validation", "invalid_request", 400);
        const auto format = harness_format_from_path(req.path);
        res.status = 400;
        res.set_content(codecs_.get(format).serialize_error_body(
            json{{"message", parse_error}, {"type", "parse_error"}}).dump(),
            "application/json");
        return;
    }
    const auto &request_policy = endpoint_policy(context.endpoint_kind);
    log_context.model = context.model; log_context.streaming = context.streaming;
    if (context.client_format == ir::ApiFormat::OpenAIResponses &&
        context.endpoint_kind != EndpointKind::ResponsesCompact &&
        !expand_responses_state(*this, codecs_, context, parse_error)) {
        log_proxy_validation_failure(log_context, "previous_response_state", 400);
        res.status = 400;
        res.set_content(codecs_.get(context.client_format).serialize_error_body(
            json{{"message", parse_error}, {"type", "invalid_request_error"},
                 {"code", "previous_response_not_found"},
                 {"param", "previous_response_id"}}).dump(),
            "application/json");
        return;
    }

    // Aggregate accounts need the request model to pick the real upstream
    // account, so resolution happens here — before the passthrough/converted
    // split.  For plain accounts this only strips the `[1m]`/`[1M]` marker.
    std::string model = context.model;
    auto cands = resolve_candidates_cached(ar.route, model);
    set_proxy_log_route_fields(log_context, model, ar.route.account_id, ar.route.local_key_id);
    if (cands.empty()) {
        log_proxy_failure(log_context, "routing", "model_unavailable", 400);
        res.status = 400;
        res.set_content(json_error("Model '" + model +
                                   "' is not available on this account", 400),
                        "application/json");
        return;
    }
    const ir::ApiFormat harness = context.client_format;
    bool conversion_needed = std::any_of(
        cands.begin(), cands.end(), [harness](const UpstreamCandidate &candidate) {
            return ir::parse_api_format(candidate.account().api_format) != harness;
        });
    if (!ensure_request_ir(codecs_, context, parse_error)) {
        log_proxy_validation_failure(log_context, "request_parse", 400);
        res.status = 400;
        res.set_content(codecs_.get(harness).serialize_error_body(
            json{{"message", parse_error}, {"type", "parse_error"}}).dump(),
            "application/json");
        return;
    }
    const std::string anthropic_beta = prepare_anthropic_beta(req, context);
    conversion_needed = conversion_needed || context.state_expanded;
    ir::ConversionContext request_conversion;
    request_conversion.source = harness;
    request_conversion.target = harness;
    std::string tool_error;
    if (!fmt::build_tool_context(context.parsed_ir.tools,
                                 request_conversion.tools, tool_error)) {
        log_proxy_validation_failure(log_context, "tool_name_collision", 422);
        res.status = 422;
        res.set_content(codecs_.get(harness).serialize_error_body(
            json{{"message", tool_error}, {"type", "unsupported_feature"},
                 {"code", "tool_name_collision"}}).dump(),
            "application/json");
        return;
    }
    if (harness == ir::ApiFormat::OpenAIResponses &&
        responses_request_needs_tool_adapter(context.parsed_ir))
        conversion_needed = true;
    if (harness == ir::ApiFormat::OpenAIResponses && conversion_needed)
        request_conversion.generated_response_id = fmt::generate_response_id();
    {
        const auto requirements = fmt::request_media_requirements(context.parsed_ir);
        std::vector<UpstreamCandidate> compatible;
        compatible.reserve(cands.size());
        std::string incompatibility;
        bool feature_failure = false;
        json unsupported_details = json::array();
        for (const auto &candidate : cands) {
            const auto target = ir::parse_api_format(candidate.account().api_format);
            // Direct same-format passthrough does not need cross-protocol
            // capability validation.  Responses namespace/tool-search
            // requests are the one same-format exception: they still need
            // the Responses item/tool adapter below.
            const bool candidate_needs_conversion =
                target != harness ||
                (harness == ir::ApiFormat::OpenAIResponses &&
                 responses_request_needs_tool_adapter(context.parsed_ir));
            auto feature_failures = candidate_needs_conversion
                ? request_feature_failures(target, harness, context.parsed_ir)
                : std::vector<RequestFeatureFailure>{};
            if (context.endpoint_kind == EndpointKind::ResponsesCompact &&
                target != ir::ApiFormat::OpenAIResponses) {
                feature_failures.push_back({
                    "responses_compact",
                    "the Responses compact endpoint requires a Responses-capable upstream"});
            }
            const bool feature_ok = feature_failures.empty();
            std::string media_reason;
            const bool media_ok = !candidate_needs_conversion ||
                fmt::target_supports_media(target, requirements, media_reason);
            if (feature_ok && media_ok)
                compatible.push_back(candidate);
            else {
                for (const auto &failure : feature_failures)
                    unsupported_details.push_back(json{
                        {"feature", failure.feature},
                        {"target", ir::to_string(target)},
                        {"reason", failure.reason}});
                if (!media_ok && !media_reason.empty())
                    unsupported_details.push_back(json{
                        {"feature", "media"}, {"target", ir::to_string(target)},
                        {"reason", media_reason}});
                if (!feature_ok) {
                    if (!feature_failure && incompatibility.empty())
                        incompatibility = feature_failures.front().reason;
                    feature_failure = true;
                } else if (incompatibility.empty()) incompatibility = media_reason;
            }
        }
        if (compatible.empty()) {
            log_proxy_validation_failure(
                log_context,
                feature_failure ? "unsupported_feature" : "unsupported_media",
                422, &unsupported_details, cands.size());
            res.status = 422;
            res.set_content(codecs_.get(harness).serialize_error_body(
                json{{"message", incompatibility.empty()
                                      ? "No configured upstream supports the request media"
                                      : incompatibility},
                     {"type", "unsupported_feature"},
                     {"code", "unsupported_feature"},
                     {"details", unsupported_details}}).dump(),
                "application/json");
            return;
        }
        cands = std::move(compatible);
    }
    RequestBodyCache body_cache(req.body, context.parsed_json, context.model,
                                ir::to_string(harness));

    // ── Streaming: defer candidate selection into the provider ─────────
    // The chunked response headers are intentionally not committed until the
    // provider writes its first event.  This lets handle_streaming try the
    // next key when an upstream returns 429/5xx before emitting any bytes.
    if (context.streaming) {
        std::shared_ptr<const ir::ChatRequest> parsed_request;
        if (conversion_needed)
            parsed_request = std::make_shared<const ir::ChatRequest>(
                context.parsed_ir);
        auto candidate_bodies = candidate_request_bodies(
            context.raw_body, context.parsed_json, context.model, cands, harness);
        std::shared_ptr<const json> parsed_json;
        if (harness == ir::ApiFormat::OpenAIResponses)
            parsed_json = std::make_shared<const json>(context.parsed_json);
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
        if (!endpoint_runner.try_reserve_accounting()) {
            log_proxy_accounting_failure(log_context);
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
        auto conversion_context = std::make_shared<const ir::ConversionContext>(
            request_conversion);
        const std::string &session_id = context.session_id;
        const auto scope = affinity_scope(ar.route.local_key_id, harness);
        size_t start = affinity_start(affinity_, scope, session_id, cands);
        handle_streaming(cands, candidate_bodies, request_id, start, session_id,
                         ar.route.account_id, ar.route.local_key_id,
                         context.endpoint_kind, harness, model,
                         std::move(parsed_json), std::move(parsed_request),
                         std::move(conversion_context),
                         context.state_expanded
                             ? std::make_shared<const std::vector<json>>(
                                   context.state_current_input)
                             : nullptr,
                         reservation, anthropic_beta, req, res, t0);
        return;
    }

    // ── Non-streaming: candidate loop with fallback ────────────────────
    const std::string &content_type = context.content_type;
    const FormatCodec &harness_codec = codecs_.get(harness);
    if (!endpoint_runner.try_reserve_accounting()) {
        log_proxy_accounting_failure(log_context);
        res.status = 503;
        res.set_header("Retry-After", "1");
        res.set_content(json_error(
            "Accounting writer is temporarily unavailable", 503),
            "application/json");
        return;
    }
    // From here the request is committed to contacting an upstream: an abnormal
    // exit below writes an internal_abort UsageEvent instead of dropping the slot.
    endpoint_runner.mark_accounting_upstream_started(
        ar.route.account_id, ar.route.local_key_id, model, false);
    ir::ChatRequest cReq;
    std::string perr;

    int concurrent_count = 0;
    bool think_filter = false;
    bool context_management_logged = false;
    ir::ApiFormat used_upstream_fmt = harness;
    const FormatCodec *upstream_codec = nullptr;
    std::optional<ir::ChatResponse> converted_response;
    const bool responses_item_adapter =
        harness == ir::ApiFormat::OpenAIResponses &&
        responses_request_needs_tool_adapter(context.parsed_ir);

    // Session-affinity spillover: start at the session's preferred candidate
    // and wrap around in fixed order (P, P+1, …, n-1, 0, …, P-1).
    const std::string &session_id = context.session_id;
    const auto scope = affinity_scope(ar.route.local_key_id, harness);
    size_t start = affinity_start(affinity_, scope, session_id, cands);
    const auto order = candidate_order(
        cands, start, routing_rr_.fetch_add(1, std::memory_order_relaxed));
    const auto base_timeouts = timeout_config_cached(request_policy.kind);
    const int budget_seconds = base_timeouts.non_streaming_timeout > 0
        ? base_timeouts.non_streaming_timeout : 600;
    const auto deadline = t0 + std::chrono::seconds(budget_seconds);

    AttemptExecutor executor(gate_);
    auto outcome = executor.execute(
        {&cands, order, deadline, budget_seconds,
         [this, request_id](const std::string &m) { return request_started(request_id, m, false); },
         [this](std::uint64_t id) { request_finished(id); },
         [&](const AttemptRequest &attempt) {
        const auto &c = attempt.candidate;
        concurrent_count = in_flight_count();

        ir::ApiFormat upstream = ir::parse_api_format(c.account().api_format);
        if (!context_management_logged &&
            context.parsed_ir.extras.contains("context_management")) {
            const auto decision = fmt::context_management_decision(
                harness, upstream,
                context.parsed_ir.extras["context_management"]);
            if (decision.action == fmt::ContextManagementAction::Drop) {
                log_context_management_downgrade(
                    log_context, harness, upstream, decision, &c);
                context_management_logged = true;
            }
        }
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
        const auto configure_forward = [&](ForwardOptions &opts) {
            if (upstream == ir::ApiFormat::Anthropic)
                opts.anthropic_beta = anthropic_beta;
        };
        if (harness == upstream && !responses_item_adapter) {
            if (harness == ir::ApiFormat::OpenAIResponses && conversion_needed) {
                cReq = context.parsed_ir;
                cReq.model = c.upstream_model();
                cReq.tools = request_conversion.tools.target_tools;
                auto same_context = request_conversion;
                same_context.target = upstream;
                const std::string body = codecs_.get(upstream).serialize_request(
                    cReq, &same_context).dump();
                result = forward_endpoint_attempt(
                    upstream_, request_policy, c, body,
                    "application/json", attempt.remaining_budget_ms,
                    attempt_timeouts, req.client_socket, nullptr,
                    configure_forward);
            } else {
                result = forward_endpoint_attempt(
                    upstream_, request_policy, c,
                    body_cache.for_candidate(c), content_type,
                    attempt.remaining_budget_ms, attempt_timeouts,
                    req.client_socket, nullptr, configure_forward);
            }
            think_filter = (upstream == ir::ApiFormat::OpenAI);
            converted_response.reset();
        } else {
            if (!ensure_request_ir(codecs_, context, perr)) {
                return proxy_request_conversion_failure(perr.empty() ? "request conversion failed" : perr);
            }
            cReq = context.parsed_ir;
            cReq.model = c.upstream_model();
            cReq.tools = request_conversion.tools.target_tools;
            upstream_codec = &codecs_.get(upstream);
            const std::string &body = body_cache.for_transformed(
                ir::to_string(upstream), c.upstream_model(), [&] {
                    auto request_context = request_conversion;
                    request_context.target = upstream;
                    return upstream_codec->serialize_request(
                        cReq, &request_context).dump();
                });
            result = forward_endpoint_attempt(
                upstream_, request_policy, c, body,
                "application/json", attempt.remaining_budget_ms,
                attempt_timeouts, req.client_socket, nullptr,
                configure_forward);
            think_filter = false;
            converted_response.reset();
            upstream_codec = &codecs_.get(upstream);
        }
        used_upstream_fmt = upstream;

        // A 2xx body in the wrong protocol is a failed candidate, not a
        // successful response that can be forwarded verbatim to the harness.
        if ((harness != upstream || responses_item_adapter) && result.success &&
            result.status_code >= 200 && result.status_code < 300) {
            ir::ChatResponse parsed;
            bool parsed_ok = false;
            perr.clear();
            try {
                auto response_context = request_conversion;
                response_context.target = upstream;
                parsed_ok = upstream_codec->parse_response(json::parse(result.body),
                                                            parsed, perr,
                                                            &response_context);
                if (parsed_ok && harness != ir::ApiFormat::OpenAIResponses) {
                    for (const auto &item : parsed.output_items) {
                        if (item.item_kind == ir::ItemKind::Opaque) {
                            parsed_ok = false;
                            perr = "upstream output Item cannot be represented by " +
                                   ir::to_string(harness);
                            break;
                        }
                        if (parsed_ok && has_raw_content(item.content)) {
                            parsed_ok = false;
                            perr = "upstream output content block cannot be represented by " +
                                   ir::to_string(harness);
                            break;
                        }
                    }
                }
            } catch (const std::exception &e) {
                perr = e.what();
            } catch (...) {
                perr = "unknown response conversion error";
            }
            if (parsed_ok) {
                converted_response = std::move(parsed);
            } else {
                result = proxy_response_protocol_failure(perr);
            }
        }
        return result;
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
    const int attempts_made = static_cast<int>(attempts.size());

    // Bind only on a successful response below. Failed keys must never become
    // the session's next preferred route.

    if (!used) {
        const int final_status = no_upstream_status(
            fwd, attempts, outcome.no_candidate_reason);
        const char *reason = proxy_terminal_failure_reason(outcome, fwd);
        log_proxy_request_final(log_context, reason, final_status, fwd.duration_ms, attempts.size(), fwd.timeout_secs, last_attempted);
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
        TerminalErrorOptions error_options;
        error_options.no_candidate_reason = outcome.no_candidate_reason;
        const auto err = render_terminal_error(
            harness_codec, &codecs_.get(used_upstream_fmt), fwd, attempts,
            error_options);
        res.status = err.status;
        if (err.close_connection) res.set_header("Connection", "close");
        if (err.retry_after_seconds > 0)
            res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
        res.set_content(err.body, "application/json");
        return;
    }

    if (fwd.client_disconnected || client_disconnected(req, 0, model)) {
        log_proxy_client_disconnect(log_context, fwd.duration_ms, attempts.size(), used);
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
    const bool response_converted = used_upstream_fmt != harness ||
        (harness == ir::ApiFormat::OpenAIResponses && responses_item_adapter);
    if (!response_converted) {
        if (fwd.success) {
            const bool response_json_valid = proxy_response_json_valid(log_context, fwd.body, fwd.status_code, fwd.duration_ms, attempts.size(), used);
            auto usage = parse_usage_for_format(ir::to_string(used_upstream_fmt),
                                                fwd.body);
            if (usage.has_value()) {
                // Prefer the model reported by the successful upstream.  A
                // provider may omit it, so fall back to the model actually
                // sent to the successful candidate (including route rewrites).
                usage->model = model_for_success_log(usage->model, *used);
                enqueue_log(used->account().id, ar.route.local_key_id,
                                     *usage, false, fwd.status_code,
                                     fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
            } else {
                if (response_json_valid)
                    log_proxy_usage_unavailable(log_context, fwd.status_code, fwd.duration_ms, attempts.size(), used);
                enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                                   model_for_success_log("", *used), false,
                                   fwd.status_code,
                                   fwd.duration_ms, used->key_slot_id,
                                   attempts_made, attempts);
            }
            if (think_filter)
                res.set_content(sanitize_response_body(fwd.body),
                                "application/json");
            else
                res.set_content(fwd.body, "application/json");
            res.status = fwd.status_code;
            if (harness == ir::ApiFormat::OpenAIResponses &&
                context.endpoint_kind != EndpointKind::ResponsesCompact)
                record_responses_state(
                    *this, context.parsed_json, fwd.body,
                    context.state_expanded ? &context.state_current_input : nullptr);
        } else {
            // Upstream error / timeout: record the failed attempt (zero
            // tokens, truthful status — 504 on timeout, else upstream code).
            enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                               model, false, fwd.status_code, fwd.duration_ms,
                               used->key_slot_id, attempts_made, attempts);
            log_proxy_request_final(log_context, proxy_failure_reason(fwd), fwd.status_code, fwd.duration_ms, attempts.size(), fwd.timeout_secs, used);
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
                auto &cResp = *converted_response;
                if (harness == ir::ApiFormat::OpenAIResponses &&
                    used_upstream_fmt != ir::ApiFormat::OpenAIResponses)
                    cResp.id = request_conversion.generated_response_id.empty()
                        ? fmt::generate_response_id()
                        : request_conversion.generated_response_id;
                auto usage_info = usage_from_ir(cResp.usage, used_upstream_fmt);
                usage_info.model = model_for_success_log(cResp.model, *used);
                enqueue_log(used->account().id, ar.route.local_key_id,
                                     usage_info, false,
                                     fwd.status_code, fwd.duration_ms, used->key_slot_id,
                                     -1, -1, -1.0, -1, -1, attempts_made, attempts);
                auto response_context = request_conversion;
                response_context.source = used_upstream_fmt;
                response_context.target = harness;
                std::string outgoing_body = harness_codec.serialize_response(
                    cResp, &response_context).dump();
                if (harness == ir::ApiFormat::OpenAIResponses &&
                    context.endpoint_kind != EndpointKind::ResponsesCompact)
                    record_responses_state(
                        *this, context.parsed_json, outgoing_body,
                        context.state_expanded ? &context.state_current_input : nullptr);
                res.status = fwd.status_code;
                res.set_content(std::move(outgoing_body), "application/json");
            } else {
                // Candidate-loop validation guarantees this is unreachable,
                // but fail closed if future code violates that invariant.
                enqueue_zero_usage(used->account().id, ar.route.local_key_id,
                                   model, false, 502, fwd.duration_ms,
                                   used->key_slot_id, attempts_made, attempts);
                log_proxy_converted_response_missing(log_context, fwd.duration_ms, attempts.size(), used);
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
            log_proxy_request_final(log_context, proxy_failure_reason(fwd), fwd.status_code, fwd.duration_ms, attempts.size(), fwd.timeout_secs, used);
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
    }
}

// ── handle_streaming ──────────────────────────────────────────────────
