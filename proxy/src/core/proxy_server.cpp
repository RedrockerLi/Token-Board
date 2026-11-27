#include "proxy_server_internal.h"
#include "format_common.h"
#include "logging.h"
#include "router.h"
#include "think_filter.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <utility>

using json = nlohmann::json;

// ── Helpers ──────────────────────────────────────────────────────────────

/// Best-effort session identifier for session-affinity routing.
std::string affinity_scope(int local_key_id, ir::ApiFormat harness) {
    return std::to_string(local_key_id) + ":" + ir::to_string(harness);
}

/// Gray-scale switch for truncated-stream classification.  When disabled, a
/// clean EOF before a terminal event is treated as success (pre-fix behavior)
/// for providers that do not emit a standard terminal frame.  Default: strict
/// classification on (providers like OpenAI/Anthropic always emit one).
bool strict_terminal_enabled() {
    const char *v = std::getenv("PROXY_STRICT_TERMINAL");
    if (v == nullptr) return true;
    return std::string(v) != "0";
}

/// Single-cycle guard: a passthrough stream stops forwarding at the first
/// terminal frame so a gateway that splices a retried generation into the same
/// response cannot make the client render two replies.  Default: on; set
/// PROXY_STREAM_CYCLE_GUARD=0 to forward upstream bytes verbatim again.
bool stream_cycle_guard_enabled() {
    const char *v = std::getenv("PROXY_STREAM_CYCLE_GUARD");
    if (v == nullptr) return true;
    return std::string(v) != "0";
}

std::optional<size_t> affinity_bound_index(
    SessionAffinity &affinity, const std::string &scope,
    const std::string &session_id,
    const std::vector<UpstreamCandidate> &cands) {
    // The binding spans EVERY candidate's key slot, not just the first
    // priority group: any account with multiple keys deserves the same
    // in-group affinity.  The returned index is a global index into `cands`;
    // candidate_order maps it to its own group's start while still trying
    // higher-priority groups first.
    if (cands.empty()) return std::nullopt;
    // Reuse one scratch buffer per worker thread: bound_index() only scans the
    // slot list, so a warmed vector keeps affinity lookup allocation-free
    // instead of building a fresh vector on every request.
    static thread_local std::vector<int> slots;
    slots.clear();
    if (slots.capacity() < cands.size()) slots.reserve(cands.size());
    for (const auto &cand : cands) slots.push_back(cand.key_slot_id);
    return affinity.bound_index(scope, session_id, slots);
}

/// Build a JSON error response.
std::string json_error(const std::string &msg, int code) {
    json j;
    const char *type = code == 401 || code == 403 ? "auth_error"
                     : code == 429 ? "rate_limit_error"
                     : code >= 500 ? "upstream_error"
                                   : "invalid_request_error";
    j["error"] = {{"message", msg}, {"type", type}, {"code", code}};
    return j.dump();
}

int stream_error_status(const json &error) {
    if (error.is_object()) {
        for (const char *field : {"status", "status_code", "code"}) {
            if (error.contains(field) && error[field].is_number_integer()) {
                const int code = error[field].get<int>();
                if (code >= 400 && code <= 599) return code;
            }
        }
        std::string classification;
        for (const char *field : {"type", "code"}) {
            if (error.contains(field) && error[field].is_string())
                classification += " " + error[field].get<std::string>();
        }
        std::transform(classification.begin(), classification.end(),
                       classification.begin(), [](unsigned char c) {
                           return static_cast<char>(std::tolower(c));
                       });
        if (classification.find("rate") != std::string::npos ||
            classification.find("quota") != std::string::npos)
            return 429;
        if (classification.find("auth") != std::string::npos ||
            classification.find("api_key") != std::string::npos)
            return 401;
        if (classification.find("permission") != std::string::npos)
            return 403;
        if (classification.find("overload") != std::string::npos)
            return 503;
    }
    return 502;
}

std::string stream_error_message(const json &error) {
    if (error.is_object() && error.contains("message") &&
        error["message"].is_string())
        return error["message"].get<std::string>();
    if (error.is_string()) return error.get<std::string>();
    return "Upstream emitted an in-stream error";
}

// ── Auth / routing helpers ───────────────────────────────────────────────

/// Extract Bearer token from request and look up the upstream route.
/// Returns 401 error info when authentication or routing fails.
AuthResult extract_and_route(const httplib::Request &req,
                                     Router &router) {
    AuthResult ar;
    std::string local_key;
    // Accept both `Authorization: Bearer <key>` (OpenAI clients, cc with
    // ANTHROPIC_AUTH_TOKEN) and `x-api-key: <key>` (Anthropic SDK clients with
    // ANTHROPIC_API_KEY).  The inbound scheme is irrelevant to routing — the
    // key only selects the upstream account.
    if (req.has_header("Authorization")) {
        std::string auth = req.get_header_value("Authorization");
        const bool bearer = auth.size() >= 7 &&
            std::tolower(static_cast<unsigned char>(auth[0])) == 'b' &&
            std::tolower(static_cast<unsigned char>(auth[1])) == 'e' &&
            std::tolower(static_cast<unsigned char>(auth[2])) == 'a' &&
            std::tolower(static_cast<unsigned char>(auth[3])) == 'r' &&
            std::tolower(static_cast<unsigned char>(auth[4])) == 'e' &&
            std::tolower(static_cast<unsigned char>(auth[5])) == 'r' &&
            auth[6] == ' ';
        if (bearer)
            local_key = auth.substr(7);
    }
    if (local_key.empty() && req.has_header("x-api-key"))
        local_key = req.get_header_value("x-api-key");
    // Gemini clients send their key in this header (or, historically, in the
    // `key` query parameter).
    if (local_key.empty() && req.has_header("x-goog-api-key"))
        local_key = req.get_header_value("x-goog-api-key");
    if (local_key.empty() && req.has_param("key"))
        local_key = req.get_param_value("key");

    if (local_key.empty()) {
        ar.status = 401;
        ar.reason = "missing_api_key";
        ar.error_json = json_error("Missing API key. "
                                    "Use: Authorization: Bearer <key>", 401);
        return ar;
    }

    // A missing routing snapshot is a service-startup condition, not a bad
    // credential: report it as a 503 so the journal and the client agree.
    if (!router.snapshot_loaded()) {
        ar.status = 503;
        ar.reason = "routing_unavailable";
        ar.error_json = json_error("Routing configuration unavailable", 503);
        return ar;
    }

    ar.route = router.route(local_key);
    if (!ar.route.success) {
        ar.status = 401;
        ar.reason = "invalid_api_key";
        ar.error_json = json_error(ar.route.error, 401);
        return ar;
    }

    ar.success = true;
    return ar;
}

// ── Model helpers ────────────────────────────────────────────────────────

/// Resolve the effective upstream candidates for a request: strip the Claude
/// Code `[1m]`/`[1M]` context-window marker (upstreams reject it), then collect
/// every matching rule reference from the immutable routing snapshot and
/// expand it into one candidate per credential.  Rule references carry only
/// indices into the snapshot's continuous upstream/credential storage, so
/// candidates never copy URLs, paths or secrets.  Returns an empty list when
/// an aggregate has no match.
std::vector<UpstreamCandidate>
resolve_candidates_uncached(Router &router, const Router::RouteResult &route,
                            std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    std::vector<UpstreamCandidate> cands;
    const auto snapshot = route.snapshot;
    if (!snapshot) return cands;
    auto refs = router.resolve_targets(route, model);
    cands.reserve(refs.size());
    // Rules that forward the client model unchanged share one request-local
    // string; a rule with a fixed target model references the snapshot's
    // interned copy instead — neither allocates per candidate.
    const auto default_model_ref =
        std::make_shared<const std::string>(model);
    for (const auto &ref : refs) {
        if (ref.upstream_index >= snapshot->upstreams.size() ||
            ref.credential_index >= snapshot->credentials.size())
            continue;
        const auto &account_ref = snapshot->upstreams[ref.upstream_index].account_ref;
        if (!account_ref || account_ref->deleted) continue;
        const auto &cred = snapshot->credentials[ref.credential_index];
        if (!cred.key_ref || cred.key_ref->key_value.empty()) continue;
        const bool fixed_model = ref.model_index < snapshot->target_models.size() &&
                                 snapshot->target_models[ref.model_index];
        TB_LOG_DEBUG("[Proxy] route_set=%d model=%s → account=%d (%s) model=%s\n",
                     route.account_id, model.c_str(), account_ref->id,
                     account_ref->name.c_str(),
                     fixed_model ? snapshot->target_models[ref.model_index]->c_str()
                                 : model.c_str());
        UpstreamCandidate c;
        c.account_ref = account_ref;
        c.key_slot_ref = cred.key_ref;
        c.usage_ref = cred.usage_ref;
        c.key_slot_id = cred.key_slot_id;
        c.upstream_model_ref =
            fixed_model ? snapshot->target_models[ref.model_index]
                        : default_model_ref;
        c.priority_group = ref.priority_group;
        cands.push_back(std::move(c));
    }
    return cands;
}
