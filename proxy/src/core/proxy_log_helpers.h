#pragma once

#include "attempt_executor.h"
#include "context_management.h"
#include "json.hpp"
#include "proxy_error_log.h"
#include "upstream_error_body.h"

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <utility>

inline ProxyLogContext make_proxy_log_context(
    std::uint64_t request_id, const std::string &method,
    const std::string &endpoint, const std::string &format) {
    ProxyLogContext context;
    context.request_id = request_id;
    context.method = method;
    context.endpoint = endpoint;
    context.format = format;
    context.skips = std::make_shared<ProxySkipTally>();
    return context;
}

/// Client identity is supplementary: it never affects routing, but a
/// structured record without it cannot tell two clients on the same local
/// key apart.
inline void set_proxy_log_client_fields(ProxyLogContext &context,
                                        const std::string &remote_addr,
                                        const std::string &user_agent) {
    context.remote_addr = remote_addr;
    context.user_agent = user_agent;
}

inline void set_proxy_log_route_fields(
    ProxyLogContext &context, const std::string &model, int route_account_id,
    int local_key_id) {
    context.model = model;
    context.route_account_id = route_account_id;
    context.local_key_id = local_key_id;
}

inline void set_proxy_log_stream_fields(
    ProxyLogContext &context, const std::string &model, int route_account_id,
    int local_key_id) {
    context.model = model;
    context.streaming = true;
    context.route_account_id = route_account_id;
    context.local_key_id = local_key_id;
}

inline std::string proxy_validation_detail_value(
    const nlohmann::json &details) {
    if (!details.is_array()) return "-";
    std::string out;
    constexpr std::size_t kMaxDetailsBytes = 768;
    for (const auto &entry : details) {
        if (!entry.is_object()) continue;
        const std::string feature = proxy_log_value(
            entry.value("feature", std::string("unknown")), 64);
        const std::string target = proxy_log_value(
            entry.value("target", std::string("unknown")), 64);
        const std::string reason = proxy_log_value(
            entry.value("reason", std::string("unspecified")), 160);
        const std::string item = feature + "@" + target + ":" + reason;
        const std::size_t separator = out.empty() ? 0 : 1;
        if (out.size() + separator + item.size() > kMaxDetailsBytes) {
            if (out.size() + separator + 3 <= kMaxDetailsBytes)
                out += out.empty() ? "..." : ",...";
            break;
        }
        if (!out.empty()) out.push_back(',');
        out += item;
    }
    return out.empty() ? "-" : out;
}

inline void log_proxy_validation_failure(
    const ProxyLogContext &context, const char *reason, int status,
    const nlohmann::json *details = nullptr,
    std::size_t candidate_count = 0) {
    if (!details) {
        log_proxy_failure(context, "request_validation", reason, status);
        return;
    }
    const std::size_t detail_count = details->is_array() ? details->size() : 0;
    const std::string extra =
        "validation_details=" + proxy_validation_detail_value(*details) +
        " validation_detail_count=" + std::to_string(detail_count) +
        " validation_candidate_count=" + std::to_string(candidate_count);
    log_proxy_failure(context, "request_validation", reason, status,
                      -1, 0, 0, false, 0, nullptr, extra);
}

inline void log_context_management_downgrade(
    const ProxyLogContext &context, ir::ApiFormat source, ir::ApiFormat target,
    const fmt::ContextManagementDecision &decision,
    const UpstreamCandidate *candidate = nullptr) {
    if (decision.action != fmt::ContextManagementAction::Drop) return;
    TB_LOG_WARN(
        "[Proxy] context_management_downgrade request_id=%llu phase=request_conversion "
        "method=%s endpoint=%s source_format=%s target_format=%s model=%s "
        "streaming=%d action=%s edit_types=%s reason=%s "
        "upstream_account_id=%d upstream_id=%d upstream_key_id=%d priority_group=%d\n",
        static_cast<unsigned long long>(context.request_id),
        proxy_log_value(context.method).c_str(),
        proxy_log_value(context.endpoint).c_str(),
        proxy_log_value(ir::to_string(source)).c_str(),
        proxy_log_value(ir::to_string(target)).c_str(),
        proxy_log_value(context.model).c_str(), context.streaming ? 1 : 0,
        fmt::context_management_action_name(decision.action),
        proxy_log_value(decision.edit_types).c_str(),
        proxy_log_value(decision.reason, 240).c_str(),
        candidate ? candidate->account().id : 0,
        candidate ? candidate->account().upstream_id : 0,
        candidate ? candidate->key_slot_id : 0,
        candidate ? candidate->priority_group : 0);
}

inline void log_proxy_auth_failure(const ProxyLogContext &context,
                                   const char *reason, int status = 401) {
    log_proxy_failure(
        context, status == 503 ? "routing" : "request_validation", reason,
        status);
}

inline void log_proxy_usage_unavailable(
    const ProxyLogContext &context, int status, int duration_ms,
    std::size_t attempts, const UpstreamCandidate *candidate,
    const std::string &event_id = {}) {
    log_proxy_failure(context, "accounting", "usage_unavailable", status,
                      duration_ms, attempts, attempts, false, 0, candidate,
                      proxy_terminal_extra(context, event_id));
}

inline void log_proxy_accounting_failure(const ProxyLogContext &context) {
    log_proxy_failure(context, "accounting", "accounting_unavailable", 503);
}

inline void log_proxy_request_final(
    const ProxyLogContext &context, const char *reason, int status,
    int duration_ms, std::size_t attempts, int timeout_secs,
    const UpstreamCandidate *candidate, int retry_after_seconds = 0,
    const std::string &event_id = {}) {
    std::string other;
    if (retry_after_seconds > 0)
        other = "retry_after_seconds=" + std::to_string(retry_after_seconds);
    log_proxy_failure(context, "request_final", reason, status, duration_ms,
                      attempts, attempts, false, timeout_secs, candidate,
                      proxy_terminal_extra(context, event_id, other));
}

inline void log_proxy_converted_response_missing(
    const ProxyLogContext &context, int duration_ms, std::size_t attempts,
    const UpstreamCandidate *candidate, const std::string &event_id = {}) {
    log_proxy_failure(context, "response_validation", "converted_response_missing",
                      502, duration_ms, attempts, attempts, false, 0, candidate,
                      proxy_terminal_extra(context, event_id));
}

inline UpstreamClient::ForwardResult proxy_request_conversion_failure(
    const std::string &error, int status_code = 400) {
    UpstreamClient::ForwardResult result;
    result.status_code = status_code;
    result.failure_kind = UpstreamFailureKind::RequestConversion;
    result.error = error;
    return result;
}

inline UpstreamClient::ForwardResult proxy_response_protocol_failure(
    const std::string &details) {
    UpstreamClient::ForwardResult result;
    result.status_code = 502;
    result.failure_kind = UpstreamFailureKind::ResponseProtocol;
    result.error = "Invalid upstream response for configured format";
    if (!details.empty()) result.error += ": " + details;
    return result;
}

inline UpstreamClient::ForwardResult proxy_stream_timeout_failure(
    int timeout_secs, const char *error) {
    UpstreamClient::ForwardResult result;
    result.status_code = 504;
    result.is_timeout = true;
    result.failure_kind = UpstreamFailureKind::Timeout;
    result.timeout_secs = timeout_secs;
    result.error = error;
    return result;
}

inline void mark_proxy_stream_protocol_failure(
    UpstreamClient::ForwardResult &result, int status, const std::string &error) {
    result.status_code = status;
    result.success = false;
    result.failure_kind = UpstreamFailureKind::StreamProtocol;
    result.is_timeout = false;
    result.timeout_secs = 0;
    result.error = error;
}

inline void mark_proxy_client_disconnect(
    UpstreamClient::ForwardResult &result) {
    result.status_code = 499;
    result.client_disconnected = true;
    result.success = false;
    result.failure_kind = UpstreamFailureKind::None;
    result.error = "Client disconnected while writing response";
}

inline AttemptExecutor::AttemptFailed proxy_attempt_failure_logger(
    ProxyLogContext context) {
    return [context = std::move(context)](
               const AttemptRequest &attempt,
               const UpstreamClient::ForwardResult &result,
               std::size_t attempt_number, bool retrying) {
        // Any real upstream error body is often the only clue (request shape,
        // model capacity, gateway, account state), so every >=400 response is
        // captured.  Keep it single-line and bounded.  The request body is
        // never recorded: it can contain prompts, tool arguments or inline
        // media.  Transport failures carry no body, so their human-readable
        // error is recorded here instead of leaving only the classified
        // reason.
        std::string extra;
        if (result.failure_kind == UpstreamFailureKind::HttpStatus &&
            result.status_code >= 400 && !result.body.empty())
            extra = "upstream_body=" + proxy_log_value(result.body, 512);
        if (is_opaque_model_400(result)) {
            if (!extra.empty()) extra += ' ';
            extra += "opaque_model_400=1";
        }
        if (!result.error.empty())
            extra += " error=" + proxy_log_value(result.error, 240);
        log_proxy_failure(context, "upstream_attempt",
                          proxy_failure_reason(result), result.status_code,
                          result.duration_ms, attempt_number, attempt_number,
                          retrying, result.timeout_secs, &attempt.candidate,
                          extra);
    };
}

inline AttemptExecutor::CandidateSkipped proxy_candidate_skip_logger(
    ProxyLogContext context) {
    return [context = std::move(context)](
               const UpstreamCandidate &candidate,
               AccountGate::KeyAcquireResult acquire_result) {
        const char *reason = proxy_gate_reason(acquire_result);
        bool first_skip = true;
        if (context.skips) {
            first_skip = context.skips->total++ == 0;
            ++context.skips->reasons[reason];
        }
        // One WARN per request; the terminal record carries the full tally.
        // Per-candidate detail stays available in foreground debug mode so a
        // storm of unavailable keys cannot flood the journal.
        if (!first_skip) {
            TB_LOG_DEBUG(
                "[Proxy] candidate skipped request_id=%llu reason=%s "
                "upstream_account_id=%d upstream_id=%d upstream_key_id=%d "
                "priority_group=%d\n",
                static_cast<unsigned long long>(context.request_id), reason,
                candidate.account().id, candidate.account().upstream_id,
                candidate.key_slot_id, candidate.priority_group);
            return;
        }
        const bool throttled =
            acquire_result == AccountGate::KeyAcquireResult::kSubscriptionCooldown ||
            acquire_result == AccountGate::KeyAcquireResult::kRateLimited;
        log_proxy_failure(context, "candidate_selection", reason,
                          throttled ? 429 : 503, -1, 0, 0, false, 0,
                          &candidate);
    };
}

inline const char *proxy_no_candidate_reason_name(
    NoCandidateReason reason) noexcept {
    switch (reason) {
        case NoCandidateReason::kLocalCapacity: return "local_capacity";
        case NoCandidateReason::kProviderQuotaCooldown:
            return "provider_quota_cooldown";
        case NoCandidateReason::kKeyRateLimited: return "key_rate_limited";
        case NoCandidateReason::kKeyUnavailable: return "key_unavailable";
        case NoCandidateReason::kSubscriptionLapsed:
            return "subscription_lapsed";
        case NoCandidateReason::kNone: break;
    }
    return "no_upstream_candidate";
}

inline const char *proxy_terminal_failure_reason(
    const AttemptOutcome &outcome,
    const UpstreamClient::ForwardResult &result) noexcept {
    if (!outcome.attempts.empty() || outcome.budget_exhausted)
        return proxy_failure_reason(result);
    return proxy_no_candidate_reason_name(outcome.no_candidate_reason);
}

inline bool proxy_response_json_valid(
    const ProxyLogContext &context, const std::string &body, int status_code,
    int duration_ms, std::size_t attempts,
    const UpstreamCandidate *candidate) {
    try {
        const auto parsed = nlohmann::json::parse(body);
        (void)parsed;
        return true;
    } catch (...) {
        log_proxy_failure(context, "response_validation",
                          "upstream_response_invalid_json", status_code,
                          duration_ms, attempts, attempts, false, 0, candidate);
        return false;
    }
}

/// The upstream delivered a second, complete message cycle after the first
/// one ended.  The guard kept the first cycle and dropped the rest; this is a
/// WARN (not a ProxyError) because the client received a complete answer.
inline void log_upstream_duplicate_cycle(
    const ProxyLogContext &context, const UpstreamCandidate &candidate,
    std::size_t dropped_bytes) {
    const std::string format = proxy_log_value(context.format);
    const std::string model = proxy_log_value(context.model);
    const std::string endpoint = proxy_log_value(context.endpoint);
    TB_LOG_WARN(
        "[Proxy] upstream_duplicate_cycle request_id=%llu format=%s model=%s "
        "endpoint=%s upstream_account_id=%d upstream_id=%d upstream_profile=%s "
        "upstream_key_id=%d dropped_bytes=%zu action=cut\n",
        static_cast<unsigned long long>(context.request_id), format.c_str(),
        model.c_str(), endpoint.c_str(), candidate.account().id,
        candidate.account().upstream_id, candidate.account().profile->name,
        candidate.key_slot_id, dropped_bytes);
}

inline void log_proxy_stream_protocol_error(
    const ProxyLogContext &context, const UpstreamCandidate &candidate,
    int duration_ms) {
    log_proxy_failure(context, "stream_parser", "stream_protocol_error", 502,
                      duration_ms, 0, 0, false, 0, &candidate);
}

inline void mark_proxy_stream_parser_exception(
    const ProxyLogContext &context, const UpstreamCandidate &candidate,
    bool &has_error, nlohmann::json &error, int &error_status) {
    log_proxy_stream_protocol_error(context, candidate, 0);
    has_error = true;
    error = nlohmann::json{{"message", "upstream stream parser failure"}};
    error_status = 502;
}

inline void log_proxy_client_disconnect(
    const ProxyLogContext &context, int duration_ms, std::size_t attempts,
    const UpstreamCandidate *candidate, const std::string &event_id = {}) {
    log_proxy_failure(context, "downstream", "client_disconnect", 499,
                      duration_ms, attempts, attempts, false, 0, candidate,
                      proxy_terminal_extra(context, event_id));
}

inline const char *proxy_stream_terminal_failure_reason(
    const AttemptOutcome &outcome,
    const UpstreamClient::ForwardResult &result,
    const nlohmann::json &stream_error) noexcept {
    if (outcome.attempts.empty())
        return proxy_no_candidate_reason_name(outcome.no_candidate_reason);
    return stream_error.is_null() ? proxy_failure_reason(result)
                                  : "stream_protocol_error";
}
