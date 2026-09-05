#include "proxy_server_internal.h"

void ProxyServer::handle_list_models(const httplib::Request &req,
                                      httplib::Response &res) {
    add_cors_headers(res);
    const auto &policy = endpoint_policy(EndpointKind::Models);

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
    auto patterns = router_.model_patterns(ar.route);
    if (!patterns.empty()) {
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

    const auto catalog_timeouts = timeout_config_cached(EndpointKind::Models);
    const int budget_seconds = catalog_timeouts.non_streaming_timeout > 0
        ? catalog_timeouts.non_streaming_timeout : 600;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(budget_seconds);

    std::vector<std::size_t> order(cands.size());
    std::iota(order.begin(), order.end(), 0);
    AttemptExecutor executor(gate_);
    auto outcome = executor.execute(
        {&cands, order, deadline, budget_seconds,
         [this](const std::string &) {
             return request_started("/v1/models", false);
         },
         [this](std::uint64_t id) { request_finished(id); },
         [&](const AttemptRequest &attempt) {
        const auto &candidate = attempt.candidate;

        return forward_endpoint_attempt(
            upstream_, policy, candidate, "", "application/json",
            static_cast<int>(attempt.remaining_budget_ms), catalog_timeouts,
            req.client_socket);
        },
        [&](const UpstreamClient::ForwardResult &result) {
            return result.client_disconnected ||
                   client_socket_gone(req.client_socket);
        }});
    auto &fwd = outcome.result;
    const auto *used = outcome.used;

    // Models records no usage, so the busy case is a render-only early return
    // (the shared renderer's attempts.empty() branch) — no accounting.
    if (!used && outcome.attempts.empty() && !fwd.is_timeout) {
        TerminalErrorOptions opts;
        opts.busy_message = "All upstream keys are busy or cooling down";
        opts.no_candidate_reason = outcome.no_candidate_reason;
        const auto err = render_terminal_error(
            codecs_.get(policy.client_format), &codecs_.get(policy.client_format),
            fwd, outcome.attempts, opts);
        res.status = err.status;
        if (err.retry_after_seconds > 0)
            res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
        res.set_content(err.body, "application/json");
        return;
    }

    res.set_header("X-Upstream-Duration-Ms", std::to_string(fwd.duration_ms));

    if (fwd.success) {
        // Real upstream model catalog, passed through unmodified — no
        // `[1m]`/`[1M]` aliases (those are internal to cc's Anthropic flow).
        res.status = fwd.status_code;
        res.set_content(fwd.body, "application/json");
    } else {
        TerminalErrorOptions opts;
        opts.used = (used != nullptr);
        const auto err = render_terminal_error(
            codecs_.get(policy.client_format), &codecs_.get(policy.client_format),
            fwd, outcome.attempts, opts);
        res.status = err.status;
        if (err.close_connection) res.set_header("Connection", "close");
        if (err.retry_after_seconds > 0)
            res.set_header("Retry-After", std::to_string(err.retry_after_seconds));
        res.set_content(err.body, "application/json");
    }
}

// ── handle_embeddings ────────────────────────────────────────────────────
