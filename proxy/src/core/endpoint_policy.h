#pragma once

#include "ir.h"

#include <string>

enum class EndpointKind {
    Chat,
    Messages,
    Responses,
    ResponsesCompact,
    Embeddings,
    Models,
    GenerateContent,
};
enum class HttpMethod { Get, Post };
enum class BodyMode { Json, Empty };
enum class ResponseMode { Codec, Raw, Stream };
enum class UsageMode { None, Json, Stream };
enum class LocalResponseMode { None, Catalog };
enum class TimeoutClass { NonStreaming, Streaming, Catalog };

struct EndpointPolicy {
    EndpointKind kind;
    const char *name;
    const char *method;
    const char *default_path;
    ir::ApiFormat client_format;
    bool allows_streaming;
    bool records_usage;
    HttpMethod http_method = HttpMethod::Post;
    BodyMode body_mode = BodyMode::Json;
    ResponseMode response_mode = ResponseMode::Codec;
    UsageMode usage_mode = UsageMode::Json;
    LocalResponseMode local_response_mode = LocalResponseMode::None;
    TimeoutClass timeout_class = TimeoutClass::NonStreaming;
};

const EndpointPolicy &endpoint_policy(EndpointKind kind);
const EndpointPolicy &endpoint_policy_for_path(const std::string &path);
const EndpointPolicy &chat_endpoint_policy(ir::ApiFormat format);

/// Model id carried by a Gemini GenerateContent path
/// (`/v1beta/models/gemini-2.5-pro:generateContent` → `gemini-2.5-pro`).
/// Empty when the path does not name a model.
std::string google_model_from_path(const std::string &path);
/// True when the path selects `:streamGenerateContent`.
bool google_stream_from_path(const std::string &path);

// Resolve the configured endpoint path once from the endpoint policy. An
// explicit path is treated as an absolute override; otherwise the policy
// supplies the endpoint suffix while preserving a base URL's existing /v1
// prefix rules for Anthropic compatibility.  Gemini carries the model and the
// streaming choice in the path, so both are inputs here.
void resolve_upstream_path(const EndpointPolicy &policy,
                           const std::string &api_format,
                           const std::string &base_url,
                           const std::string &endpoint_path,
                           const std::string &model,
                           bool streaming,
                           std::string &out_path,
                           bool &out_path_is_full);
