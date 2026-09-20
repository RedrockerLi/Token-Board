#pragma once

#include "attempt_executor.h"
#include "json.hpp"
#include "proxy_error_log.h"

#include <cstddef>
#include <cstdint>
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
    return context;
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

inline void log_proxy_validation_failure(
    const ProxyLogContext &context, const char *reason, int status) {
    log_proxy_failure(context, "request_validation", reason, status);
}

inline void log_proxy_auth_failure(const ProxyLogContext &context,
                                   bool has_authorization,
                                   bool has_api_key) {
    log_proxy_validation_failure(
        context, has_authorization || has_api_key ? "invalid_api_key"
                                                   : "missing_api_key", 401);
}

inline void log_proxy_usage_unavailable(
    const ProxyLogContext &context, int status, int duration_ms,
    std::size_t attempts, const UpstreamCandidate *candidate) {
    log_proxy_failure(context, "accounting", "usage_unavailable", status,
                      duration_ms, attempts, attempts, false, 0, candidate);
}

inline void log_proxy_accounting_failure(const ProxyLogContext &context) {
    log_proxy_failure(context, "accounting", "accounting_unavailable", 503);
}

inline void log_proxy_request_final(
    const ProxyLogContext &context, const char *reason, int status,
    int duration_ms, std::size_t attempts, int timeout_secs,
    const UpstreamCandidate *candidate) {
    log_proxy_failure(context, "request_final", reason, status, duration_ms,
                      attempts, attempts, false, timeout_secs, candidate);
}

inline void log_proxy_converted_response_missing(
    const ProxyLogContext &context, int duration_ms, std::size_t attempts,
    const UpstreamCandidate *candidate) {
    log_proxy_failure(context, "response_validation", "converted_response_missing",
                      502, duration_ms, attempts, attempts, false, 0, candidate);
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
        log_proxy_failure(context, "upstream_attempt",
                          proxy_failure_reason(result), result.status_code,
                          result.duration_ms, attempt_number, attempt_number,
                          retrying, result.timeout_secs, &attempt.candidate);
    };
}

inline AttemptExecutor::CandidateSkipped proxy_candidate_skip_logger(
    ProxyLogContext context) {
    return [context = std::move(context)](
               const UpstreamCandidate &candidate,
               AccountGate::KeyAcquireResult acquire_result) {
        const int status = acquire_result ==
                AccountGate::KeyAcquireResult::kSubscriptionCooldown
            ? 429 : 503;
        log_proxy_failure(context, "candidate_selection",
                          proxy_gate_reason(acquire_result), status, -1, 0, 0,
                          false, 0, &candidate);
    };
}

inline const char *proxy_terminal_failure_reason(
    const AttemptOutcome &outcome,
    const UpstreamClient::ForwardResult &result) noexcept {
    if (!outcome.attempts.empty() || outcome.budget_exhausted)
        return proxy_failure_reason(result);
    return outcome.no_candidate_reason ==
               NoCandidateReason::kProviderQuotaCooldown
        ? "provider_quota_cooldown"
        : outcome.no_candidate_reason == NoCandidateReason::kLocalCapacity
            ? "local_capacity" : "no_upstream_candidate";
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
    const UpstreamCandidate *candidate) {
    log_proxy_failure(context, "downstream", "client_disconnect", 499,
                      duration_ms, attempts, attempts, false, 0, candidate);
}

inline const char *proxy_stream_terminal_failure_reason(
    const AttemptOutcome &outcome,
    const UpstreamClient::ForwardResult &result,
    const nlohmann::json &stream_error) noexcept {
    if (outcome.attempts.empty())
        return outcome.no_candidate_reason ==
                   NoCandidateReason::kProviderQuotaCooldown
            ? "provider_quota_cooldown"
            : outcome.no_candidate_reason == NoCandidateReason::kLocalCapacity
                ? "local_capacity" : "no_upstream_candidate";
    return stream_error.is_null() ? proxy_failure_reason(result)
                                  : "stream_protocol_error";
}
