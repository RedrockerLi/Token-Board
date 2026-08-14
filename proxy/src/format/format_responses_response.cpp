#include "format_responses_internal.h"

using namespace ir;

bool ResponsesCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err) const {
    if (!in.is_object()) {
        err = "Responses response must be a JSON object";
        return false;
    }
    if (in.contains("id") && in["id"].is_string())
        out.id = in["id"].get<std::string>();
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("status") && in["status"].is_string())
        out.stop_reason = fmt::responses_status_to_stop(in["status"].get<std::string>());

    if (in.contains("output") && in["output"].is_array()) {
        for (const auto &item : in["output"]) {
            if (!item.is_object()) continue;
            std::string type = item.value("type", "");
            if (type == "message") {
                if (item.contains("content"))
                    parse_responses_content(item["content"], out.content);
            } else if (type == "function_call") {
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.tool_call_id = item.value("call_id", "");
                b.tool_name = item.value("name", "");
                if (item.contains("arguments") && item["arguments"].is_string())
                    b.tool_input = parse_responses_arguments(item["arguments"].get<std::string>());
                out.content.push_back(std::move(b));
            } else if (type == "custom_tool_call") {
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.extra["type"] = "custom";
                b.tool_call_id = item.value("call_id", "");
                b.tool_name = item.value("name", "custom_tool");
                b.tool_input = json{{"input", item.value("input", "")}};
                out.content.push_back(std::move(b));
            } else if (type == "reasoning") {
                if (item.contains("summary") && item["summary"].is_array()) {
                    for (const auto &s : item["summary"]) {
                        if (s.is_object() && s.value("type", "") == "summary_text" &&
                            s.contains("text") && s["text"].is_string()) {
                            ContentBlock b;
                            b.kind = ContentKind::Thinking;
                            b.text = s["text"].get<std::string>();
                            out.content.push_back(std::move(b));
                        }
                    }
                }
            }
        }
    }

    if (in.contains("usage") && in["usage"].is_object()) {
        const json &u = in["usage"];
        if (u.contains("input_tokens") && u["input_tokens"].is_number_integer())
            out.usage.prompt_tokens = u["input_tokens"].get<int>();
        if (u.contains("output_tokens") && u["output_tokens"].is_number_integer())
            out.usage.completion_tokens = u["output_tokens"].get<int>();
        if (u.contains("total_tokens") && u["total_tokens"].is_number_integer())
            out.usage.total_tokens = u["total_tokens"].get<int>();
        else
            out.usage.total_tokens = out.usage.prompt_tokens + out.usage.completion_tokens;
        out.usage.cache_read_tokens =
            fmt::read_cache_hit_tokens(u, out.usage.prompt_tokens).value_or(0);
    }
    out.extras["created_at"] = in.contains("created_at") ? in["created_at"] : json(nullptr);
    out.extras["object"] = in.value("object", "response");
    if (in.contains("status")) out.extras["status"] = in["status"];
    return true;
}

json ResponsesCodec::serialize_response(const ir::ChatResponse &in) const {
    json out;
    out["id"] = in.id.empty() ? "resp-proxy" : in.id;
    out["object"] = "response";  // forced — never inherit source format
    out["created_at"] = in.extras.contains("created_at") ? in.extras["created_at"]
                                                         : json(nullptr);
    out["status"] = fmt::stop_reason_to_responses(in.stop_reason);
    out["model"] = in.model;

    json output = json::array();
    json msg_items;  // accumulate message content blocks per message
    std::string text_content;
    json msg_parts = json::array();
    json tool_items = json::array();
    json reasoning_items = json::array();
    for (const auto &b : in.content) {
        switch (b.kind) {
            case ContentKind::Text:
                msg_parts.push_back(fmt::filter_keys(json{{"type", "output_text"}, {"text", b.text}}, {"type", "text"}));
                break;
            case ContentKind::Thinking:
                reasoning_items.push_back(json{{"type", "summary_text"}, {"text", b.text}});
                break;
            case ContentKind::Image:
                // The Responses API has no generic assistant output-image
                // content item corresponding to an OpenAI/Anthropic image
                // input block. Do not invent a wire shape clients cannot
                // parse; image-generation output uses a separate tool item.
                break;
            case ContentKind::ToolUse: {
                json item;
                const bool custom = b.extra.value("type", "") == "custom";
                item["type"] = custom ? "custom_tool_call" : "function_call";
                item["id"] = b.tool_call_id.empty() ? "fc_" + std::to_string((uintptr_t)&b)
                                                     : b.tool_call_id;
                item["call_id"] = b.tool_call_id;
                item["name"] = b.tool_name;
                if (custom)
                    item["input"] = b.tool_input.value("input", "");
                else
                    item["arguments"] = b.tool_input.dump();
                item["status"] = "completed";
                output.push_back(std::move(item));
                break;
            }
            case ContentKind::ToolResult:
                break;
        }
    }
    if (!msg_parts.empty()) {
        json item;
        item["id"] = "msg_0";
        item["type"] = "message";
        item["status"] = "completed";
        item["role"] = "assistant";
        item["content"] = std::move(msg_parts);
        output.push_back(std::move(item));
    }
    if (!reasoning_items.empty()) {
        json item;
        item["id"] = "rs_0";
        item["type"] = "reasoning";
        item["summary"] = std::move(reasoning_items);
        output.push_back(std::move(item));
    }
    out["output"] = std::move(output);

    json usage;
    usage["input_tokens"] = in.usage.prompt_tokens;
    usage["output_tokens"] = in.usage.completion_tokens;
    usage["total_tokens"] = in.usage.total_tokens;
    if (in.usage.cache_read_tokens > 0) {
        usage["input_tokens_details"] = json::object();
        usage["input_tokens_details"]["cached_tokens"] = in.usage.cache_read_tokens;
    }
    out["usage"] = std::move(usage);
    return out;
}

// ── Streaming: parser (upstream Responses SSE → IR events) ──────────────
