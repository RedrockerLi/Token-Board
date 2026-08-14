#include "endpoint_policy.h"

#include <array>

namespace {
constexpr std::array<EndpointPolicy, 5> policies{{
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
    {EndpointKind::Embeddings, "embeddings", "POST", "/embeddings",
     ir::ApiFormat::OpenAI, false, true, HttpMethod::Post, BodyMode::Json,
     ResponseMode::Codec, UsageMode::Json, LocalResponseMode::None,
     TimeoutClass::NonStreaming},
    {EndpointKind::Models, "models", "GET", "/models",
     ir::ApiFormat::OpenAI, false, false, HttpMethod::Get, BodyMode::Empty,
     ResponseMode::Raw, UsageMode::None, LocalResponseMode::Catalog,
     TimeoutClass::Catalog},
}};
}

const EndpointPolicy &endpoint_policy(EndpointKind kind) {
    return policies[static_cast<std::size_t>(kind)];
}

const EndpointPolicy &endpoint_policy_for_path(const std::string &path) {
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
    return endpoint_policy(EndpointKind::Chat);
}

void resolve_upstream_path(const EndpointPolicy &policy,
                           const std::string &api_format,
                           const std::string &base_url,
                           const std::string &endpoint_path,
                           std::string &out_path,
                           bool &out_path_is_full) {
    if (!endpoint_path.empty()) {
        out_path = endpoint_path;
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

    const auto format = ir::parse_api_format(api_format);
    if (policy.kind == EndpointKind::Embeddings ||
        policy.kind == EndpointKind::Models) {
        out_path = policy.default_path;
    } else if (format == ir::ApiFormat::Anthropic) {
        out_path = (base_path.size() >= 3 &&
                    base_path.compare(base_path.size() - 3, 3, "/v1") == 0)
            ? "/messages" : "/v1/messages";
    } else if (format == ir::ApiFormat::OpenAIResponses) {
        out_path = "/responses";
    } else {
        // The path is selected by the target upstream format, not by the
        // client's endpoint policy. This matters for cross-format chat and
        // messages forwarding (e.g. Anthropic client -> OpenAI upstream).
        out_path = "/chat/completions";
    }
}
