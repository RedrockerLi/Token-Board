#pragma once

#include "upstream_client.h"

#include <string>

#include "json.hpp"

/// opencode.ai Zen/Go intermittently answers HTTP 400 with a body that only
/// names the model (for example {"model":"deepseek-v4.1-flash"}, 31 bytes),
/// masking the real rejection reason.  Public reports replay identical bytes
/// and get 200/400/200, so that shape is a transient gateway fault rather
/// than a verdict on the request.  This predicate is wire-format independent:
/// it looks only at the status and the JSON body.
inline bool is_model_only_error_body(const std::string &body) {
    // Cheap prefilter: a JSON object carrying a model key always contains
    // this literal, so ordinary error bodies never reach the parser.
    if (body.find("\"model\"") == std::string::npos) return false;
    const auto parsed = nlohmann::json::parse(body, nullptr, false);
    return !parsed.is_discarded() && parsed.is_object() &&
           parsed.contains("model") && !parsed.contains("error");
}

inline bool is_opaque_model_400(const UpstreamClient::ForwardResult &result) {
    return result.status_code == 400 &&
           result.failure_kind == UpstreamFailureKind::HttpStatus &&
           !result.body.empty() && !result.body_truncated &&
           !result.body_too_large && is_model_only_error_body(result.body);
}
