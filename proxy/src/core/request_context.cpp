#include "request_context.h"

#include "endpoint_policy.h"
#include "request_timing.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

namespace {
std::string session_id(const httplib::Request &request,
                       const nlohmann::json &body) {
    std::string value = request.get_header_value("x-session-id");
    if (value.empty()) value = request.get_header_value("x-conversation-id");
    if (!value.empty()) return value;
    if (body.contains("user") && body["user"].is_string())
        return body["user"].get<std::string>();
    if (body.contains("metadata") && body["metadata"].is_object()) {
        const auto &metadata = body["metadata"];
        if (metadata.contains("user_id") && metadata["user_id"].is_string())
            return metadata["user_id"].get<std::string>();
    }
    if (body.contains("previous_response_id") &&
        body["previous_response_id"].is_string())
        return body["previous_response_id"].get<std::string>();
    return {};
}
}

bool parse_request_context(const httplib::Request &request,
                           RequestContext &context, std::string &error) {
    const auto &policy = endpoint_policy_for_path(request.path);
    context.client_format = policy.client_format;
    context.raw_body = std::make_shared<const std::string>(request.body);
    context.queue_ms = current_request_queue_delay_ms();
    context.content_type = request.has_header("Content-Type")
        ? request.get_header_value("Content-Type") : "application/json";
    try {
        context.parsed_json = nlohmann::json::parse(request.body);
    } catch (...) {
        error = "invalid JSON body";
        return false;
    }
    if (!context.parsed_json.is_object()) {
        error = "request body must be a JSON object";
        return false;
    }
    if (!context.parsed_json.contains("model") ||
        !context.parsed_json["model"].is_string() ||
        context.parsed_json["model"].get_ref<const std::string &>().empty()) {
        error = "model must be a non-empty string";
        return false;
    }
    context.model = context.parsed_json["model"].get<std::string>();
    if (context.parsed_json.contains("stream") &&
        !context.parsed_json["stream"].is_boolean()) {
        error = "stream must be a boolean";
        return false;
    }
    context.streaming = context.parsed_json.value("stream", false);
    if (context.streaming && !policy.allows_streaming) {
        error = "streaming is not supported for this endpoint";
        return false;
    }
    context.session_id = session_id(request, context.parsed_json);
    return true;
}

bool ensure_request_ir(const CodecRegistry &codecs, RequestContext &context,
                       std::string &error) {
    if (context.ir_ready) return true;
    if (!codecs.get(context.client_format).parse_request(
            context.parsed_json, context.parsed_ir, error))
        return false;
    context.ir_ready = true;
    return true;
}
