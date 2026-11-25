#include "endpoint_policy.h"

#include <array>

namespace {

/// Text after the last ':' of the final path segment ("" when none).  Gemini
/// carries its RPC in the path this way: `.../models/x:generateContent`.
std::string method_token(const std::string &path) {
    const size_t slash = path.rfind('/');
    const size_t colon = path.find(':', slash == std::string::npos ? 0 : slash);
    if (colon == std::string::npos) return {};
    return path.substr(colon + 1);
}

/// The `{model}` segment of `.../models/{model}:method`, "" when absent.
std::string model_segment(const std::string &path) {
    static const std::string marker = "/models/";
    const size_t position = path.rfind(marker);
    if (position == std::string::npos) return {};
    const size_t start = position + marker.size();
    size_t end = path.find(':', start);
    if (end == std::string::npos) end = path.size();
    return path.substr(start, end - start);
}

std::string bare_model(const std::string &model) {
    static const std::string prefix = "models/";
    if (model.size() > prefix.size() &&
        model.compare(0, prefix.size(), prefix) == 0)
        return model.substr(prefix.size());
    return model;
}

void substitute(std::string &text, const std::string &token,
                const std::string &value) {
    size_t position = 0;
    while ((position = text.find(token, position)) != std::string::npos) {
        text.replace(position, token.size(), value);
        position += value.size();
    }
}

/// A base URL whose path already names an API version takes the model suffix
/// directly; anything else gets the documented `v1beta` prefix.
bool base_path_has_version(const std::string &base_path) {
    for (const char *version : {"/v1beta", "/v1alpha", "/v1"}) {
        const std::string suffix = version;
        if (base_path.size() >= suffix.size() &&
            base_path.compare(base_path.size() - suffix.size(), suffix.size(),
                              suffix) == 0)
            return true;
    }
    return false;
}

constexpr std::array<EndpointPolicy, 7> policies{{
    {EndpointKind::Chat, "chat", "POST", "/chat/completions",
     ir::ApiFormat::OpenAI, true, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::Streaming},
    {EndpointKind::Messages, "messages", "POST", "/messages",
     ir::ApiFormat::Anthropic, true, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::Streaming},
    {EndpointKind::Responses, "responses", "POST", "/responses",
     ir::ApiFormat::OpenAIResponses, true, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::Streaming},
    {EndpointKind::ResponsesCompact, "responses_compact", "POST",
     "/responses/compact", ir::ApiFormat::OpenAIResponses, true, true,
     HttpMethod::Post, BodyMode::Json, ResponseMode::Codec, UsageMode::Json,
     LocalResponseMode::None, TimeoutClass::Streaming},
    {EndpointKind::Embeddings, "embeddings", "POST", "/embeddings",
     ir::ApiFormat::OpenAI, false, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::NonStreaming},
    {EndpointKind::Models, "models", "GET", "/models",
     ir::ApiFormat::OpenAI, false, false, HttpMethod::Get, BodyMode::Empty,
     ResponseMode::Raw, UsageMode::None, LocalResponseMode::Catalog,
     TimeoutClass::Catalog},
    // Gemini's endpoint path is built per request from the model and the RPC,
    // so this policy carries no static default_path.
    {EndpointKind::GenerateContent, "generate", "POST", "",
     ir::ApiFormat::Google, true, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::Streaming},
}};

}  // namespace

const EndpointPolicy &endpoint_policy(EndpointKind kind) {
    return policies[static_cast<std::size_t>(kind)];
}

std::string google_model_from_path(const std::string &path) {
    if (method_token(path).empty()) return {};
    return bare_model(model_segment(path));
}

bool google_stream_from_path(const std::string &path) {
    return method_token(path) == "streamGenerateContent";
}

const EndpointPolicy &endpoint_policy_for_path(const std::string &path) {
    const std::string method = method_token(path);
    if (method == "streamGenerateContent" || method == "generateContent")
        return endpoint_policy(EndpointKind::GenerateContent);
    if (path.size() >= 18 &&
        path.compare(path.size() - 18, 18, "/responses/compact") == 0)
        return endpoint_policy(EndpointKind::ResponsesCompact);
    if (path.size() >= 9 && path.compare(path.size() - 9, 9, "/messages") == 0)
        return endpoint_policy(EndpointKind::Messages);
    if (path.size() >= 10 && path.compare(path.size() - 10, 10, "/responses") == 0)
        return endpoint_policy(EndpointKind::Responses);
    if (path.size() >= 11 && path.compare(path.size() - 11, 11, "/embeddings") == 0)
        return endpoint_policy(EndpointKind::Embeddings);
    if (path.size() >= 7 && path.compare(path.size() - 7, 7, "/models") == 0)
        return endpoint_policy(EndpointKind::Models);
    return endpoint_policy(EndpointKind::Chat);
}

const EndpointPolicy &chat_endpoint_policy(ir::ApiFormat format) {
    if (format == ir::ApiFormat::Anthropic)
        return endpoint_policy(EndpointKind::Messages);
    if (format == ir::ApiFormat::OpenAIResponses)
        return endpoint_policy(EndpointKind::Responses);
    if (format == ir::ApiFormat::Google)
        return endpoint_policy(EndpointKind::GenerateContent);
    return endpoint_policy(EndpointKind::Chat);
}

void resolve_upstream_path(const EndpointPolicy &policy,
                           const std::string &api_format,
                           const std::string &base_url,
                           const std::string &endpoint_path,
                           const std::string &model,
                           bool streaming,
                           std::string &out_path,
                           bool &out_path_is_full) {
    const auto format = ir::parse_api_format(api_format);
    const std::string method =
        streaming ? "streamGenerateContent" : "generateContent";

    if (!endpoint_path.empty()) {
        out_path = endpoint_path;
        // An override that keeps the model in the path can name it abstractly;
        // a plain path is taken verbatim.
        substitute(out_path, "{model}", bare_model(model));
        substitute(out_path, "{method}", method);
        out_path_is_full = true;
        return;
    }

    out_path_is_full = false;
    std::string base_path;
    const size_t scheme_end = base_url.find("://");
    if (scheme_end != std::string::npos) {
        const size_t host_start = scheme_end + 3;
        const size_t path_start = base_url.find('/', host_start);
        if (path_start != std::string::npos)
            base_path = base_url.substr(path_start);
    }
    while (base_path.size() > 1 && base_path.back() == '/')
        base_path.pop_back();

    if (policy.kind == EndpointKind::Embeddings ||
        policy.kind == EndpointKind::Models) {
        out_path = policy.default_path;
    } else if (format == ir::ApiFormat::Google) {
        // The model is a path variable on this wire and the RPC selects the
        // streaming transport.
        out_path = base_path_has_version(base_path) ? "" : "/v1beta";
        out_path += "/models/" + bare_model(model) + ":" + method;
        if (streaming) out_path += "?alt=sse";
    } else if (format == ir::ApiFormat::Anthropic) {
        out_path = (base_path.size() >= 3 &&
                    base_path.compare(base_path.size() - 3, 3, "/v1") == 0)
            ? "/messages" : "/v1/messages";
    } else if (format == ir::ApiFormat::OpenAIResponses) {
        out_path = policy.kind == EndpointKind::ResponsesCompact
            ? "/responses/compact" : "/responses";
    } else {
        // The path is selected by the target upstream format, not by the
        // client's endpoint policy. This matters for cross-format chat and
        // messages forwarding (e.g. Anthropic client -> OpenAI upstream).
        out_path = "/chat/completions";
    }
}
