#pragma once

#include "codec.h"
#include "endpoint_policy.h"

#include <string>
#include <memory>
#include <vector>

namespace httplib { struct Request; }

struct RequestContext {
    std::shared_ptr<const std::string> raw_body;
    ir::ApiFormat client_format = ir::ApiFormat::OpenAI;
    EndpointKind endpoint_kind = EndpointKind::Chat;
    std::string model;
    bool streaming = false;
    std::string session_id;
    std::string previous_response_id;
    bool state_expanded = false;
    // For an expanded Responses request, this is the caller's new input
    // only.  The serialized parsed_json contains the replayed parent chain;
    // state recording must not append that chain a second time.
    std::vector<nlohmann::json> state_current_input;
    std::string content_type = "application/json";
    int queue_ms = 0;
    nlohmann::json parsed_json;
    ir::ChatRequest parsed_ir;
    bool ir_ready = false;
};

bool parse_request_context(const httplib::Request &request,
                           RequestContext &context, std::string &error);
bool ensure_request_ir(const CodecRegistry &codecs, RequestContext &context,
                       std::string &error);

/// Session id forwarded to an "opencode-go" upstream, in precedence order:
///   1. the client's own `x-opencode-session` header, passed through;
///   2. the request's session id (native headers first, then body identity);
///   3. a conversation fingerprint over system + first user message;
///   4. a fresh random id.
/// Only (1) and (2) are free; the fingerprint hashes the request body and the
/// random fallback draws from OpenSSL, so callers should only ask for this
/// value when they actually need to forward it.  A candidate that would make
/// an unsafe header line (control characters, oversized) is rejected and the
/// next source is used instead.
std::string opencode_session_value(const httplib::Request &request,
                                   const RequestContext &context);

/// One-off `tb-<32 hex>` session id for callers with no downstream session to
/// carry (model catalog passthrough, cooldown probe).
std::string opencode_random_session_value();
