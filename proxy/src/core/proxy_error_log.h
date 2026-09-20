#pragma once

#include "account_gate.h"
#include "candidate_selection.h"
#include "logging.h"
#include "upstream_client.h"

#include <algorithm>
#include <cctype>
#include <cstddef>
#include <cstdint>
#include <string>

/// Request-local fields shared by every proxy error log.  Deliberately does
/// not contain credentials, headers or request/response bodies.
struct ProxyLogContext {
    std::uint64_t request_id = 0;
    std::string method;
    std::string endpoint;
    std::string format;
    std::string model;
    bool streaming = false;
    int route_account_id = 0;
    int local_key_id = 0;
};

// cpp-httplib calls its logger after a route handler has returned. Keep a
// request-thread marker so the logger can cover parser/framework failures
// without duplicating the structured record already emitted by a business
// handler. The pre-routing hook resets it for every parsed request.
inline thread_local bool proxy_error_log_emitted = false;

inline void reset_proxy_error_log_scope() noexcept {
    proxy_error_log_emitted = false;
}

inline bool consume_proxy_error_log_scope() noexcept {
    const bool emitted = proxy_error_log_emitted;
    proxy_error_log_emitted = false;
    return emitted;
}

/// Keep journal fields single-line and bounded.  Models and paths are allowed
/// to contain punctuation, but whitespace, '=' and control bytes would make a
/// key=value record ambiguous or multi-line.
inline std::string proxy_log_value(const std::string &value,
                                   std::size_t limit = 160) {
    std::string out;
    out.reserve(std::min(value.size(), limit));
    for (const unsigned char byte : value) {
        if (out.size() >= limit) break;
        if (std::isalnum(byte) || byte == '_' || byte == '-' || byte == '.' ||
            byte == '/' || byte == ':' || byte == ',' || byte == '[' ||
            byte == ']' || byte == '*' || byte == '@') {
            out.push_back(static_cast<char>(byte));
        } else {
            out.push_back('_');
        }
    }
    return out.empty() ? "-" : out;
}

inline const char *proxy_failure_reason(
    const UpstreamClient::ForwardResult &result) noexcept {
    if (result.client_disconnected)
        return "client_disconnect";
    switch (result.failure_kind) {
        case UpstreamFailureKind::Configuration: return "upstream_configuration";
        case UpstreamFailureKind::OriginCapacity: return "upstream_origin_capacity";
        case UpstreamFailureKind::DnsFailure: return "dns_failure";
        case UpstreamFailureKind::DnsTimeout: return "dns_timeout";
        case UpstreamFailureKind::DnsNoAddress: return "dns_no_address";
        case UpstreamFailureKind::Connect: return "connect_failure";
        case UpstreamFailureKind::Tls: return "tls_failure";
        case UpstreamFailureKind::Write: return "upstream_write_failure";
        case UpstreamFailureKind::Read: return "upstream_read_failure";
        case UpstreamFailureKind::Timeout: return "upstream_timeout";
        case UpstreamFailureKind::HttpStatus: return "upstream_http_error";
        case UpstreamFailureKind::ResponseTooLarge: return "upstream_response_too_large";
        case UpstreamFailureKind::StreamTruncated: return "stream_truncated";
        case UpstreamFailureKind::StreamProtocol: return "stream_protocol_error";
        case UpstreamFailureKind::RequestConversion: return "request_conversion";
        case UpstreamFailureKind::ResponseProtocol: return "upstream_response_protocol";
        case UpstreamFailureKind::Transport: return "upstream_transport_failure";
        case UpstreamFailureKind::None: break;
    }
    if (result.is_timeout) return "upstream_timeout";
    if (result.status_code >= 400) return "upstream_http_error";
    return "upstream_failure";
}

inline const char *proxy_gate_reason(
    AccountGate::KeyAcquireResult result) noexcept {
    switch (result) {
        case AccountGate::KeyAcquireResult::kConcurrencyFull:
            return "local_capacity";
        case AccountGate::KeyAcquireResult::kSubscriptionCooldown:
            return "provider_quota_cooldown";
        case AccountGate::KeyAcquireResult::kAcquired:
            break;
    }
    return "candidate_unavailable";
}

inline void log_proxy_failure(
    const ProxyLogContext &context, const char *phase, const char *reason,
    int status_code, int duration_ms = -1, std::size_t attempt = 0,
    std::size_t attempts = 0, bool retrying = false, int timeout_secs = 0,
    const UpstreamCandidate *candidate = nullptr,
    const std::string &extra_fields = {}) {
    proxy_error_log_emitted = true;
    const std::string method = proxy_log_value(context.method);
    const std::string endpoint = proxy_log_value(context.endpoint);
    const std::string format = proxy_log_value(context.format);
    const std::string model = proxy_log_value(context.model);
    const std::string suffix = extra_fields.empty() ? std::string()
                                                     : " " + extra_fields;
    TB_LOG_WARN(
        "[ProxyError] request_id=%llu phase=%s method=%s endpoint=%s "
        "format=%s model=%s streaming=%d route_account_id=%d local_key_id=%d "
        "status=%d reason=%s attempt=%zu attempts=%zu retrying=%d "
        "duration_ms=%d timeout_secs=%d upstream_account_id=%d upstream_id=%d "
        "upstream_key_id=%d priority_group=%d%s\n",
        static_cast<unsigned long long>(context.request_id), phase, method.c_str(),
        endpoint.c_str(), format.c_str(), model.c_str(), context.streaming ? 1 : 0,
        context.route_account_id, context.local_key_id, status_code, reason,
        attempt, attempts, retrying ? 1 : 0, duration_ms, timeout_secs,
        candidate ? candidate->account().id : 0,
        candidate ? candidate->account().upstream_id : 0,
        candidate ? candidate->key_slot_id : 0,
        candidate ? candidate->priority_group : 0, suffix.c_str());
}
