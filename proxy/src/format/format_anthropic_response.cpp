#include "format_anthropic_internal.h"

using namespace ir;

bool AnthropicCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err) const {
    if (!in.is_object()) {
        err = "Anthropic response must be a JSON object";
        return false;
    }
    if (in.contains("id") && in["id"].is_string())
        out.id = in["id"].get<std::string>();
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("content") && in["content"].is_array())
        parse_anthropic_blocks(in["content"], out.content);
    if (in.contains("stop_reason") && in["stop_reason"].is_string()) {
        std::string sr = in["stop_reason"].get<std::string>();
        out.stop_reason = fmt::anthropic_stop_reason_to_stop(sr);
        if (sr == "stop_sequence" && in.contains("stop_sequence") &&
            in["stop_sequence"].is_string())
            out.stop_sequence = in["stop_sequence"].get<std::string>();
    }
    if (in.contains("usage") && in["usage"].is_object()) {
        const json &u = in["usage"];
        if (u.contains("input_tokens") && u["input_tokens"].is_number_integer())
            out.usage.prompt_tokens = u["input_tokens"].get<int>();
        if (u.contains("output_tokens") && u["output_tokens"].is_number_integer())
            out.usage.completion_tokens = u["output_tokens"].get<int>();
        if (u.contains("cache_read_input_tokens") && u["cache_read_input_tokens"].is_number_integer())
            out.usage.cache_read_tokens = u["cache_read_input_tokens"].get<int>();
        if (u.contains("cache_creation_input_tokens") && u["cache_creation_input_tokens"].is_number_integer())
            out.usage.cache_creation_tokens = u["cache_creation_input_tokens"].get<int>();
        out.usage.total_tokens = out.usage.prompt_tokens + out.usage.completion_tokens;
    }
    if (in.contains("type")) out.extras["type"] = in["type"];
    if (in.contains("role")) out.extras["role"] = in["role"];
    return true;
}

json AnthropicCodec::serialize_response(const ir::ChatResponse &in) const {
    json out;
    out["id"] = in.id.empty() ? "msg-proxy" : in.id;
    out["type"] = "message";  // forced — never inherit source format
    out["role"] = "assistant";
    out["model"] = in.model;
    out["content"] = serialize_anthropic_blocks(in.content);
    out["stop_reason"] = fmt::stop_reason_to_anthropic(in.stop_reason);
    if (in.stop_sequence.has_value())
        out["stop_sequence"] = *in.stop_sequence;
    else
        out["stop_sequence"] = nullptr;
    json usage;
    usage["input_tokens"] = in.usage.prompt_tokens;
    usage["output_tokens"] = in.usage.completion_tokens;
    usage["cache_read_input_tokens"] = in.usage.cache_read_tokens;
    usage["cache_creation_input_tokens"] = in.usage.cache_creation_tokens;
    out["usage"] = std::move(usage);
    return out;
}

// ── Streaming: parser (upstream Anthropic SSE → IR events) ──────────────
