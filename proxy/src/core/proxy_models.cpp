#include "proxy_server_internal.h"

#include <unordered_set>

void ProxyServer::handle_list_models(const httplib::Request &req,
                                      httplib::Response &res) {
    tb_http_debug::downstream_request(req);
    add_cors_headers(res);
    const auto &policy = endpoint_policy(EndpointKind::Models);
    const auto request_id = allocate_request_id();
    auto log_context = make_proxy_log_context(
        request_id, req.method, req.path, ir::to_string(policy.client_format));

    auto ar = extract_and_route(req, router_);
    if (!ar.success) {
        log_proxy_auth_failure(log_context, req.has_header("Authorization"),
                               req.has_header("x-api-key"));
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

    // A plain account serves the catalog the dashboard maintains in
    // upstream_model_catalog ("更新模型" fills it; the table's generation
    // triggers hot-reload the snapshot).  This is a pure local read — no
    // upstream request, no per-key gate, no failover: the stored list is the
    // authoritative answer for clients.  An account that was never refreshed
    // answers with an empty list.
    json out = json::array();
    if (ar.route.snapshot &&
        ar.route.route_set_index < ar.route.snapshot->route_sets.size()) {
        const auto &set =
            ar.route.snapshot->route_sets[ar.route.route_set_index];
        std::unordered_set<std::string> seen;
        for (std::uint32_t i = set.rule_start;
             i < set.rule_start + set.rule_count; ++i) {
            const auto &rule = ar.route.snapshot->route_rules[i];
            if (rule.upstream_index >= ar.route.snapshot->upstreams.size())
                continue;
            const auto &catalog = ar.route.snapshot
                ->upstreams[rule.upstream_index].catalog_ref;
            if (!catalog) continue;
            for (const auto &model : *catalog) {
                if (!seen.insert(model).second) continue;
                out.push_back(json{{"id", model}, {"object", "model"},
                                   {"created", 1}, {"owned_by", "token-board"}});
            }
        }
    }
    res.status = 200;
    res.set_content(json{{"object", "list"}, {"data", std::move(out)}}.dump(),
                    "application/json");
}

// ── handle_embeddings ────────────────────────────────────────────────────
