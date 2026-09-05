#include "proxy_server_internal.h"
void ProxyServer::handle_streaming(
    const std::vector<UpstreamCandidate> &cands,
    const std::vector<CandidateRequestBody> &candidate_bodies, size_t start,
    const std::string &session_id, int local_key_id, ir::ApiFormat harness,
    const std::string &resolved_model, std::shared_ptr<const json> parsed_json,
    std::shared_ptr<const ir::ChatRequest> parsed_request, std::shared_ptr<const ir::ConversionContext> conversion_context,
    std::shared_ptr<const std::vector<json>> state_current_input, std::shared_ptr<UsageReservation> reservation,
    const std::string &anthropic_beta, const httplib::Request &req,
    httplib::Response &res, std::chrono::steady_clock::time_point t0) {
    const FormatCodec &harness_codec = codecs_.get(harness);
    const std::string content_type = req.has_header("Content-Type") ? req.get_header_value("Content-Type") : "application/json";
    const std::string scope = affinity_scope(local_key_id, harness);
    const auto order = candidate_order(cands, start, routing_rr_.fetch_add(1, std::memory_order_relaxed));
    const auto base_timeouts = timeout_config_cached(chat_endpoint_policy(harness).kind);
    const int budget_seconds = base_timeouts.streaming_first_byte_timeout > 0 ? base_timeouts.streaming_first_byte_timeout : 60; const auto deadline = t0 + std::chrono::seconds(budget_seconds);
    res.set_chunked_content_provider(
        "text/event-stream",
        [this, cands, candidate_bodies, order, session_id, scope, local_key_id,
         harness, resolved_model, parsed_json, parsed_request, conversion_context,
         state_current_input, reservation,
         anthropic_beta,
         base_timeouts, deadline, budget_seconds,
         content_type, t0, &res,
         client_sock = req.client_socket](size_t, httplib::DataSink &sink) -> bool {
            const FormatCodec &out_codec = codecs_.get(harness); std::uint64_t inflight_id = 0;
            adopt_accounting_reservation(reservation);
            mark_accounting_upstream_started(
                cands.empty() ? 0 : cands.front().account().id, local_key_id,
                resolved_model, true);
            auto reservation_guard = make_scope_exit(
                [this] { release_unconsumed_accounting(); });
            auto inflight_guard = make_scope_exit(
                [this, &inflight_id] { request_finished(inflight_id); });
            const UpstreamCandidate *used = nullptr; UpstreamClient::ForwardResult final_result;
            bool committed = false, last_timeout = false; int last_status = 429;
            int last_account_id = 0, last_slot_id = 0, last_duration_ms = 0, attempts_made = 0;
            std::vector<Database::AttemptInfo> attempts; json last_stream_error;
            bool first_semantic = true;
            std::chrono::steady_clock::time_point first_semantic_at, last_semantic_at;
            int upstream_semantic_ttft = -1;
            bool client_write_failed = false, terminal_error_forwarded = false;
            bool source_terminal_seen = false; ir::Usage final_stream_usage;
            json responses_terminal; fmt::SseFrameBuffer responses_state_sse;
            AttemptExecutor attempt_executor(gate_);
            std::unordered_map<std::string, std::string> converted_bodies;
            auto write_to_sink = [&](const std::string &data) -> bool {
                if (data.empty()) return true;
                bool ok = sink.write(data.data(), data.size());
                if (ok) {
                    committed = true;
                    if (harness == ir::ApiFormat::OpenAIResponses) {
                        responses_state_sse.feed(data.data(), data.size(),
                            [&](const std::string &frame) {
                                std::string event_name, payload;
                                if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
                                try {
                                    const json j = json::parse(payload);
                                    const std::string type = j.value("type", "");
                                    if ((type == "response.completed" ||
                                         type == "response.incomplete") &&
                                        j.contains("response"))
                                        responses_terminal = j["response"];
                                } catch (...) {}
                            });
                    }
                } else {
                    client_write_failed = true;
                }
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
            auto outcome = attempt_executor.execute(
                {&cands, order, deadline, budget_seconds,
                 {}, {},
                 [&](const AttemptRequest &attempt_request) {
                const auto &candidate = attempt_request.candidate;
                terminal_error_forwarded = false;
                if (inflight_id == 0) {
                    inflight_id = request_started(candidate.upstream_model(), true);
                }
                const auto upstream = ir::parse_api_format(candidate.account().api_format);
                const bool responses_item_adapter = harness == ir::ApiFormat::OpenAIResponses &&
                    parsed_request && responses_request_needs_tool_adapter(*parsed_request);
                const bool passthrough = harness == upstream &&
                    !responses_item_adapter;
                const bool filter_thinking = passthrough && upstream == ir::ApiFormat::OpenAI;
                auto attempt_timeouts = base_timeouts;
                if (!clamp_to_remaining_budget(attempt_timeouts, deadline, true)) {
                    UpstreamClient::ForwardResult result;
                    result.status_code = 504;
                    result.is_timeout = true;
                    result.timeout_secs = budget_seconds;
                    result.error = "stream retry budget exhausted";
                    return result;
                }
                ++attempts_made; const std::string *body = nullptr;
                if (passthrough) {
                    if (harness == ir::ApiFormat::OpenAIResponses && parsed_request) {
                        auto converted = *parsed_request;
                        converted.model = candidate.upstream_model();
                        if (conversion_context)
                            converted.tools = conversion_context->tools.target_tools;
                        const std::string cache_key =
                            std::to_string(static_cast<int>(upstream)) + "\n" + candidate.upstream_model();
                        auto found = converted_bodies.find(cache_key);
                        if (found == converted_bodies.end())
                        found = converted_bodies.emplace(
                            cache_key,
                            codecs_.get(upstream).serialize_request(
                                converted, conversion_context.get()).dump()).first;
                        body = &found->second;
                    } else {
                    const auto &cached =
                        candidate_bodies[attempt_request.candidate_index];
                    if (cached) {
                        body = cached.get();
                    } else {
                        if (!parsed_json) {
                            UpstreamClient::ForwardResult result;
                            result.status_code = 502;
                            result.error = "missing shared request JSON";
                            return result;
                        }
                        const std::string cache_key =
                            std::to_string(static_cast<int>(upstream)) + "\n" +
                            candidate.upstream_model();
                        auto found = converted_bodies.find(cache_key);
                        if (found == converted_bodies.end()) {
                            auto changed = *parsed_json;
                            changed["model"] = candidate.upstream_model();
                            found = converted_bodies.emplace(
                                cache_key, changed.dump()).first;
                        }
                        body = &found->second;
                    }
                    }
                } else {
                    const std::string cache_key =
                        std::to_string(static_cast<int>(upstream)) + "\n" +
                        candidate.upstream_model();
                    auto found = converted_bodies.find(cache_key);
                    if (found == converted_bodies.end()) {
                        if (!parsed_request) {
                            UpstreamClient::ForwardResult result;
                            result.status_code = 502;
                            result.error = "missing shared request IR";
                            return result;
                        }
                        auto converted = *parsed_request;
                        converted.model = candidate.upstream_model();
                        if (conversion_context)
                            converted.tools = conversion_context->tools.target_tools;
                        found = converted_bodies.emplace(
                            cache_key,
                            codecs_.get(upstream).serialize_request(
                                converted, conversion_context.get()).dump()
                        ).first;
                    }
                    body = &found->second;
                }
                auto response_context = conversion_context
                    ? std::make_shared<ir::ConversionContext>(*conversion_context)
                    : std::make_shared<ir::ConversionContext>();
                response_context->source = upstream;
                response_context->target = harness;
                if (response_context->source == response_context->target)
                    response_context->generated_response_id.clear();
                auto parser = codecs_.get(upstream).make_stream_parser(
                    response_context.get());
                std::unique_ptr<ir::StreamEmitter> emitter;
                ThinkStreamFilter think_filter;
                bool attempt_first_semantic = true;
                auto attempt_semantic_seen =
                    std::make_shared<std::atomic<bool>>(false);
                auto attempt_semantic_progress =
                    std::make_shared<std::atomic<std::uint64_t>>(0);
                auto attempt_terminal_seen =
                    std::make_shared<std::atomic<bool>>(false);
                std::chrono::steady_clock::time_point attempt_first_semantic_at;
                std::chrono::steady_clock::time_point attempt_last_semantic_at;
                bool attempt_has_stream_error = false;
                json attempt_stream_error;
                int attempt_stream_error_status = 502;
                ir::Usage attempt_usage;
                if (!passthrough) {
                    emitter = out_codec.make_stream_emitter(
                        response_context.get());
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
                        return false;
                    }
                    if (event.type == ir::StreamEventType::UsageEvent ||
                        event.type == ir::StreamEventType::MessageStart)
                        ir::usage_merge(attempt_usage, event.usage);
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
                auto on_combined_event = [&](const ir::StreamEvent &event) -> bool {
                    const bool metrics_ok = on_metrics_event(event);
                    if (!metrics_ok && event.type == ir::StreamEventType::ErrorEvent &&
                        !committed)
                        return false;
                    return on_event(event);
                };
                bool metrics_enabled = true;
                auto on_chunk = [&](const char *data, size_t len) -> bool {
                    if (metrics_enabled) {
                        try {
                            const bool observed = passthrough
                                ? parser->feed(data, len, on_metrics_event)
                                : true;
                            if (!observed && !attempt_has_stream_error) {
                                metrics_enabled = false;
                            }
                        } catch (const std::exception &e) {
                            TB_LOG_WARN(
                                    "[Proxy] metrics stream parser disabled: %s\n",
                                    e.what());
                            metrics_enabled = false;
                        } catch (...) {
                            TB_LOG_WARN(
                                    "[Proxy] metrics stream parser disabled\n");
                            metrics_enabled = false;
                        }
                    }
                    if (attempt_has_stream_error && !committed) return false;
                    bool forwarded = true;
                    if (!passthrough) {
                        forwarded = parser->feed(data, len, on_combined_event);
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
                auto result = forward_endpoint_attempt(
                    upstream_, chat_endpoint_policy(harness), candidate, *body,
                    passthrough ? content_type : "application/json",
                    attempt_request.remaining_budget_ms, attempt_timeouts,
                    client_sock, on_chunk,
                    [&](ForwardOptions &opts) {
                        opts.semantic_seen = attempt_semantic_seen;
                        opts.semantic_progress = attempt_semantic_progress;
                        opts.streaming_body_buffer_limit = 256 * 1024;
                        if (upstream == ir::ApiFormat::Anthropic)
                            opts.anthropic_beta = anthropic_beta;
                        if (strict_terminal_enabled())
                            opts.terminal_seen = attempt_terminal_seen;
                    });
                    if (metrics_enabled || !passthrough) {
                    try {
                        const ir::StreamParser::EmitFn finish_events =
                            passthrough ? ir::StreamParser::EmitFn(on_metrics_event)
                                         : ir::StreamParser::EmitFn(on_combined_event);
                        parser->finish(finish_events);
                    } catch (const std::exception &e) {
                        TB_LOG_WARN(
                                "[Proxy] metrics stream finish ignored: %s\n",
                                e.what());
                    } catch (...) {
                        TB_LOG_WARN(
                                "[Proxy] metrics stream finish ignored\n");
                    }
                }
                if (attempt_has_stream_error && !result.client_disconnected) {
                    result.status_code = attempt_stream_error_status;
                    result.success = false;
                    result.is_timeout = false;
                    result.timeout_secs = 0;
                    result.error = stream_error_message(attempt_stream_error);
                }
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
                    if (write_attempt("\n\n")) terminal_error_forwarded = true;
                }
                if (result.success && result.status_code >= 200 && result.status_code < 300) {
                    if (!passthrough) {
                        emitter->finish(write_attempt);
                    }
                    if (!attempt_first_semantic) {
                        upstream_semantic_ttft = static_cast<int>(
                            std::chrono::duration_cast<std::chrono::milliseconds>(
                                attempt_first_semantic_at - attempt_started).count());
                    }
                    final_stream_usage = attempt_usage;
                    source_terminal_seen = attempt_terminal_seen->load(
                        std::memory_order_acquire);
                    return result;
                }
                last_timeout = result.is_timeout;
                last_status = result.status_code;
                last_account_id = candidate.account().id;
                last_slot_id = candidate.key_slot_id;
                last_duration_ms = result.duration_ms;
                last_stream_error = attempt_has_stream_error
                    ? attempt_stream_error : json();
                return result;
                },
                [&](const UpstreamClient::ForwardResult &result) {
                    return result.client_disconnected || client_write_failed ||
                           (committed && !result.success) ||
                           client_socket_gone(client_sock);
                }});
            used = outcome.used;
            final_result = std::move(outcome.result);
            attempts = std::move(outcome.attempts);
            attempts_made = static_cast<int>(attempts.size());
            if (outcome.last_attempted) {
                last_account_id = outcome.last_attempted->account().id;
                last_slot_id = outcome.last_attempted->key_slot_id;
            }
            last_timeout = final_result.is_timeout;
            last_status = final_result.status_code;
            last_duration_ms = final_result.duration_ms;
            inflight_guard.run_now();
            if (used && client_write_failed) {
                enqueue_zero_usage(used->account().id, local_key_id,
                                   resolved_model, true, 499,
                                   final_result.duration_ms, used->key_slot_id,
                                   static_cast<int>(attempts.size()), attempts,
                                   final_result.duration_ms);
                return false;
            }
            // AttemptOutcome::used identifies the terminal candidate even when
            // its attempt failed. Only a successful terminal attempt may take
            // the success/accounting path below.
            if (used && final_result.success) {
                auto usage = usage_from_ir(
                    final_stream_usage,
                    ir::parse_api_format(used->account().api_format));
                usage.model = resolved_model;
                const int proxy_ttft = first_semantic ? -1 : static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        first_semantic_at - t0).count());
                const int generation_ms = first_semantic ? -1 : static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        last_semantic_at - first_semantic_at).count());
                const double output_tps = generation_ms > 0 && usage.completion_tokens > 1
                    ? (usage.completion_tokens - 1) * 1000.0 / generation_ms : -1.0;
                enqueue_log(used->account().id, local_key_id, usage, true,
                                     final_result.status_code, final_result.duration_ms, used->key_slot_id,
                                     proxy_ttft, generation_ms, output_tps,
                                     upstream_semantic_ttft, final_result.duration_ms,
                                     attempts_made, attempts);
                affinity_.bind(scope, session_id, used->key_slot_id);
                if (harness == ir::ApiFormat::OpenAIResponses && parsed_json &&
                    source_terminal_seen)
                    responses_state_sse.finish([&](const std::string &frame) {
                        std::string event_name, payload;
                        if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
                        try {
                            const json j = json::parse(payload);
                            if ((j.value("type", "") == "response.completed" ||
                                 j.value("type", "") == "response.incomplete") &&
                                j.contains("response")) responses_terminal = j["response"];
                        } catch (...) {}
                    });
                sink.done();
                if (harness == ir::ApiFormat::OpenAIResponses &&
                    parsed_json && source_terminal_seen &&
                    !responses_terminal.is_null())
                    record_responses_state(*this, *parsed_json,
                                           responses_terminal.dump(),
                                           state_current_input ? state_current_input.get()
                                                                : nullptr);
                return true;
            }
            if (client_write_failed || final_result.client_disconnected ||
                client_socket_gone(client_sock)) {
                if (last_account_id) {
                    enqueue_zero_usage(last_account_id, local_key_id,
                                       resolved_model, true, 499,
                                       last_duration_ms, last_slot_id,
                                       static_cast<int>(attempts.size()),
                                       attempts, final_result.duration_ms);
                }
                return false;
            }
            const auto err = render_stream_error(
                &codecs_.get(harness), final_result, attempts, last_timeout,
                last_stream_error.is_null() ? json() : last_stream_error,
                last_status, outcome.no_candidate_reason);
            res.status = err.status;
            if (err.retry_after_seconds > 0)
                res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
            if (err.close_connection) {
                // A timeout invalidates the in-flight stream. Remove the
                // default keep-alive header before committing the deferred
                // error response so clients do not reuse this connection.
                res.headers.erase("Keep-Alive");
                res.headers.erase("Connection");
                res.set_header("Connection", "close");
            }
            if (last_account_id) {
                enqueue_zero_usage(last_account_id, local_key_id,
                                   resolved_model, true, err.status,
                                   last_duration_ms, last_slot_id,
                                   attempts_made, attempts,
                                   final_result.duration_ms);
            } else {
                enqueue_zero_usage(cands.front().account().id, local_key_id,
                                   resolved_model, true, err.status, 0, 0,
                                   0, attempts);
            }
            if (!committed || !terminal_error_forwarded) {
                const json body = err.passthrough
                    ? (last_stream_error.is_null()
                        ? normalized_error_body(err) : last_stream_error)
                    : normalized_error_body(err);
                emit_error(body);
            }
            sink.done();
            return true;
        },
        nullptr);
    res.set_deferred_chunked_headers(); }
