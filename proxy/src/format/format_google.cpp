#include "format_google_internal.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>

namespace {

/// google.rpc.Code value and symbolic status for one HTTP status.  Gemini
/// error bodies carry both, and clients read either.
struct GrpcStatus {
    int code;
    const char *status;
};

GrpcStatus grpc_status_for(int http_status, const std::string &type) {
    switch (http_status) {
        case 400: return {3, "INVALID_ARGUMENT"};
        case 401: return {16, "UNAUTHENTICATED"};
        case 402: return {9, "FAILED_PRECONDITION"};
        case 403: return {7, "PERMISSION_DENIED"};
        case 404: return {5, "NOT_FOUND"};
        case 409: return {6, "ALREADY_EXISTS"};
        case 416: return {11, "OUT_OF_RANGE"};
        case 429: return {8, "RESOURCE_EXHAUSTED"};
        case 499: return {1, "CANCELLED"};
        case 500: return {13, "INTERNAL"};
        case 501: return {12, "UNIMPLEMENTED"};
        case 503: return {14, "UNAVAILABLE"};
        case 504: return {4, "DEADLINE_EXCEEDED"};
        default: break;
    }
    if (type == "rate_limit_error") return {8, "RESOURCE_EXHAUSTED"};
    if (type == "timeout_error") return {4, "DEADLINE_EXCEEDED"};
    if (type == "invalid_request_error") return {3, "INVALID_ARGUMENT"};
    if (type == "service_unavailable") return {14, "UNAVAILABLE"};
    return {2, "UNKNOWN"};
}

/// `google.rpc.RetryInfo.retryDelay` is a Duration such as "16s" / "1.5s".
int retry_delay_seconds(const json &details) {
    if (!details.is_array()) return 0;
    for (const auto &detail : details) {
        if (!detail.is_object()) continue;
        if (!detail.contains("retryDelay") || !detail["retryDelay"].is_string())
            continue;
        const std::string raw = detail["retryDelay"].get<std::string>();
        if (raw.empty()) continue;
        char *end = nullptr;
        const double value = std::strtod(raw.c_str(), &end);
        if (end == raw.c_str() || !std::isfinite(value) || value <= 0) continue;
        if (*end != '\0' && *end != 's') continue;
        const double rounded = std::ceil(value);
        if (rounded < 1) return 1;
        if (rounded > 86400) return 86400;
        return static_cast<int>(rounded);
    }
    return 0;
}

}  // namespace

std::unique_ptr<ir::StreamParser> GoogleCodec::make_stream_parser(
    const ir::ConversionContext *context) const {
    return gfmt::make_stream_parser_impl(context);
}

std::unique_ptr<ir::StreamEmitter> GoogleCodec::make_stream_emitter(
    const ir::ConversionContext *context) const {
    return gfmt::make_stream_emitter_impl(context);
}

json GoogleCodec::parse_error_body(const json &value) const {
    json error = value;
    if (error.is_object() && error.contains("error") &&
        error["error"].is_object())
        error = error["error"];
    else
        error = fmt::normalize_error_body(value);
    if (!error.is_object()) return fmt::normalize_error_body(value);

    json normalized;
    normalized["message"] = error.value("message", "upstream error");
    const std::string status = error.value("status", "UPSTREAM_ERROR");
    std::string type = status;
    std::transform(type.begin(), type.end(), type.begin(),
                   [](unsigned char ch) {
                       return static_cast<char>(std::tolower(ch));
                   });
    normalized["type"] = type;
    normalized["code"] = error.contains("code") ? error["code"] : json(502);
    if (error.contains("details")) {
        normalized["details"] = error["details"];
        const int retry_after = retry_delay_seconds(error["details"]);
        if (retry_after > 0) normalized["retry_after_seconds"] = retry_after;
    }
    return normalized;
}

json GoogleCodec::serialize_error_body(const json &value) const {
    const int http_status = value.contains("code") &&
            value["code"].is_number_integer()
        ? value["code"].get<int>() : 502;
    const std::string type = value.value("type", "upstream_error");
    const GrpcStatus status = grpc_status_for(http_status, type);
    json error;
    error["code"] = status.code;
    error["message"] = value.value("message", "upstream error");
    error["status"] = status.status;
    if (value.contains("details")) error["details"] = value["details"];
    return json{{"error", std::move(error)}};
}

std::unique_ptr<FormatCodec> make_google_codec() {
    return std::make_unique<GoogleCodec>();
}
