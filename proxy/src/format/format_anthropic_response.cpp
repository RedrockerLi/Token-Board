#include "format_anthropic_internal.h"

using namespace ir;

bool AnthropicCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err,
                                    const ir::ConversionContext *context) const {
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
    if (!out.content.empty()) {
        // Preserve the provider's block order. Consecutive ordinary blocks
        // share one assistant group and are merged by downstream Chat/
        // Anthropic serializers; reasoning and tool blocks remain distinct
        // Items at their original positions.
        AgentItem message;
        message.item_kind = ItemKind::Message;
        message.role = "assistant"; message.group_id = 1;
        auto flush_message = [&] {
            if (!message.content.empty()) {
                out.output_items.push_back(std::move(message));
                message = AgentItem{};
                message.item_kind = ItemKind::Message;
                message.role = "assistant"; message.group_id = 1;
            }
        };
        for (const auto &b : out.content) {
            if (b.kind == ContentKind::Text || b.kind == ContentKind::Image ||
                b.kind == ContentKind::File || b.kind == ContentKind::Audio) {
                message.content.push_back(b);
            } else if (b.kind == ContentKind::Thinking) {
                flush_message();
                AgentItem reasoning;
                reasoning.item_kind = ItemKind::Reasoning;
                reasoning.role = "assistant"; reasoning.group_id = 1;
                reasoning.content.push_back(b);
                if (b.extra.contains("signature"))
                    reasoning.extra["signature"] = b.extra["signature"];
                if (b.extra.contains("responses_reasoning_item") &&
                    b.extra["responses_reasoning_item"].is_object())
                    reasoning.extra["raw_item"] = b.extra["responses_reasoning_item"];
                out.output_items.push_back(std::move(reasoning));
            } else if (b.kind == ContentKind::ToolUse) {
                flush_message();
                if (!b.tool_input.is_object()) {
                    err = "upstream Anthropic tool_use input must be an object";
                    return false;
                }
                ContentBlock mapped = b;
                if (context) {
                    for (const auto &mapping : context->tools.mappings) {
                        if (mapping.flat_name != mapped.tool_name) continue;
                        mapped.tool_name = mapping.original_name;
                        if (mapping.kind == ToolKind::Custom) {
                            mapped.extra["type"] = "custom";
                            mapped.tool_input = json{{"input", mapped.tool_input.value("input", "")}};
                        } else if (mapping.kind == ToolKind::ToolSearch) {
                            mapped.tool_name = "tool_search";
                        }
                        break;
                    }
                }
                AgentItem call;
                call.item_kind = ItemKind::FunctionCall;
                call.role = "assistant"; call.group_id = 1;
                call.call_id = mapped.tool_call_id;
                call.name = mapped.tool_name;
                call.payload = mapped.tool_input.dump();
                if (context) {
                    for (const auto &mapping : context->tools.mappings) {
                        if (mapping.flat_name != b.tool_name) continue;
                        call.name = mapping.original_name;
                        call.namespace_name = mapping.namespace_name;
                        if (mapping.kind == ToolKind::Custom) {
                            call.item_kind = ItemKind::CustomToolCall;
                            call.payload = mapped.tool_input.value("input", "");
                        } else if (mapping.kind == ToolKind::ToolSearch) {
                            call.item_kind = ItemKind::ToolSearchCall;
                            call.execution = json{{"type", "client"}};
                        }
                        break;
                    }
                }
                out.output_items.push_back(std::move(call));
            }
        }
        flush_message();
    }
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

json AnthropicCodec::serialize_response(const ir::ChatResponse &in,
                                        const ir::ConversionContext *) const {
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
