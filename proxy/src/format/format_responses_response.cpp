#include "format_responses_internal.h"

using namespace ir;

bool ResponsesCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err,
                                    const ir::ConversionContext *context) const {
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
            AgentItem ordered;
            ordered.id = item.value("id", "");
            ordered.status = item.value("status", "");
            ordered.phase = item.value("phase", "");
            ordered.call_id = item.value("call_id", "");
            ordered.name = item.value("name", "");
            ordered.namespace_name = item.value("namespace", "");
            ordered.encrypted_content = item.value("encrypted_content", "");
            ordered.execution = item.value("execution", json::object());
            ordered.extra["raw_item"] = item;
            if (type == "message") {
                ordered.item_kind = ItemKind::Message;
                ordered.role = item.value("role", "assistant");
                if (item.contains("content"))
                    parse_responses_content(item["content"], out.content);
                if (item.contains("content"))
                    parse_responses_content(item["content"], ordered.content);
            } else if (type == "function_call") {
                ordered.item_kind = ItemKind::FunctionCall;
                if (item.contains("arguments") && !item["arguments"].is_string()) {
                    err = "upstream function_call arguments must be a JSON string";
                    return false;
                }
                ordered.payload = item.value("arguments", "");
                if (context) {
                    for (const auto &mapping : context->tools.mappings) {
                        if (mapping.flat_name != ordered.name) continue;
                        ordered.name = mapping.original_name;
                        ordered.namespace_name = mapping.namespace_name;
                        if (mapping.kind == ToolKind::Custom) {
                            ordered.item_kind = ItemKind::CustomToolCall;
                            try { ordered.payload = json::parse(ordered.payload).value("input", ""); }
                            catch (...) { err = "invalid JSON in upstream custom tool arguments"; return false; }
                        } else if (mapping.kind == ToolKind::ToolSearch) {
                            ordered.item_kind = ItemKind::ToolSearchCall;
                            ordered.execution = json{{"type", "client"}};
                        }
                        break;
                    }
                }
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.tool_call_id = item.value("call_id", "");
                b.tool_name = ordered.name;
                if (item.contains("arguments") && item["arguments"].is_string()) {
                    b.extra["raw_arguments"] = item["arguments"];
                    try { b.tool_input = json::parse(item["arguments"].get<std::string>()); }
                    catch (...) { err = "invalid JSON in upstream function_call arguments"; return false; }
                }
                if (ordered.item_kind == ItemKind::CustomToolCall)
                    b.extra["type"] = "custom";
                else if (ordered.item_kind == ItemKind::ToolSearchCall) {
                    if (!b.tool_input.is_object()) {
                        err = "upstream tool_search arguments must be an object";
                        return false;
                    }
                    b.tool_name = "tool_search";
                    b.tool_input = item.value("arguments", json::object());
                    b.extra["type"] = "function";
                }
                out.content.push_back(std::move(b));
            } else if (type == "custom_tool_call") {
                ordered.item_kind = ItemKind::CustomToolCall;
                if (item.contains("input") && !item["input"].is_string()) {
                    err = "upstream custom_tool_call input must be a string";
                    return false;
                }
                ordered.payload = item.value("input", "");
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.extra["type"] = "custom";
                b.tool_call_id = item.value("call_id", "");
                b.tool_name = item.value("name", "custom_tool");
                b.tool_input = json{{"input", item.value("input", "")}};
                out.content.push_back(std::move(b));
            } else if (type == "function_call_output") {
                ordered.item_kind = ItemKind::FunctionCallOutput;
                ordered.role = "tool";
                if (item.contains("output")) fmt::parse_media_content(item["output"], ordered.content, true);
            } else if (type == "custom_tool_call_output") {
                ordered.item_kind = ItemKind::CustomToolCallOutput;
                ordered.role = "tool";
                if (item.contains("output")) fmt::parse_media_content(item["output"], ordered.content, true);
            } else if (type == "tool_search_call") {
                ordered.item_kind = ItemKind::ToolSearchCall;
                if (item.contains("arguments") && !item["arguments"].is_object()) {
                    err = "upstream tool_search_call arguments must be an object";
                    return false;
                }
                ordered.payload = item.value("arguments", json::object()).dump();
                ordered.role = "assistant";
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.tool_call_id = ordered.call_id;
                b.tool_name = "tool_search";
                b.tool_input = item.value("arguments", json::object());
                b.extra["type"] = "function";
                out.content.push_back(std::move(b));
            } else if (type == "tool_search_output") {
                ordered.item_kind = ItemKind::ToolSearchOutput;
                ordered.role = "tool";
                if (item.contains("output")) fmt::parse_media_content(item["output"], ordered.content, true);
            } else if (type == "reasoning") {
                ordered.item_kind = ItemKind::Reasoning;
                std::string visible_summary;
                ordered.role = "assistant";
                if (item.contains("summary") && item["summary"].is_array()) {
                    for (const auto &s : item["summary"]) {
                        if (s.is_object() && s.value("type", "") == "summary_text" &&
                            s.contains("text") && s["text"].is_string()) {
                            visible_summary += s["text"].get<std::string>();
                            ContentBlock b;
                            b.kind = ContentKind::Thinking;
                            b.text = s["text"].get<std::string>();
                            b.extra["responses_reasoning_item"] = item;
                            ordered.content.push_back(std::move(b));
                        }
                    }
                }
                if (!visible_summary.empty()) {
                    ContentBlock visible;
                    visible.kind = ContentKind::Thinking;
                    visible.text = std::move(visible_summary);
                    visible.extra["responses_reasoning_item"] = item;
                    out.content.push_back(std::move(visible));
                }
                if (ordered.content.empty() && !ordered.encrypted_content.empty()) {
                    ContentBlock b;
                    b.kind = ContentKind::Thinking;
                    b.extra["responses_reasoning_item"] = item;
                    ordered.content.push_back(b);
                    out.content.push_back(std::move(b));
                }
            } else {
                ordered.item_kind = ItemKind::Opaque;
            }
            out.output_items.push_back(std::move(ordered));
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

json ResponsesCodec::serialize_response(const ir::ChatResponse &in,
                                        const ir::ConversionContext *context) const {
    json out;
    const std::string response_id = in.id.empty()
        ? (context && !context->generated_response_id.empty()
               ? context->generated_response_id : fmt::generate_response_id())
        : in.id;
    out["id"] = response_id;
    out["object"] = "response";  // forced — never inherit source format
    out["created_at"] = in.extras.contains("created_at") ? in.extras["created_at"]
                                                         : json(nullptr);
    out["status"] = fmt::stop_reason_to_responses(in.stop_reason);
    out["model"] = in.model;

    json output = json::array();
    if (!in.output_items.empty()) {
        std::size_t index = 0;
        for (const auto &item : in.output_items) {
            if (item.extra.contains("raw_item") && item.extra["raw_item"].is_object()) {
                json raw = item.extra["raw_item"];
                if (context && raw.value("type", "") == "function_call") {
                    for (const auto &mapping : context->tools.mappings) {
                        if (mapping.flat_name != raw.value("name", "")) continue;
                        raw["name"] = mapping.original_name;
                        if (!mapping.namespace_name.empty()) raw["namespace"] = mapping.namespace_name;
                        if (mapping.kind == ToolKind::Custom) {
                            raw["type"] = "custom_tool_call";
                            if (raw.contains("arguments") && raw["arguments"].is_string()) {
                                try { raw["input"] = json::parse(raw["arguments"].get<std::string>()).value("input", ""); }
                                catch (...) {}
                                raw.erase("arguments");
                            }
                        } else if (mapping.kind == ToolKind::ToolSearch) {
                            raw["type"] = "tool_search_call";
                            raw["execution"] = "client";
                            if (raw.contains("arguments") && raw["arguments"].is_string()) {
                                try { raw["arguments"] = json::parse(raw["arguments"].get<std::string>()); }
                                catch (...) { /* parser rejected malformed upstream arguments */ }
                            }
                        }
                        break;
                    }
                }
                if (!raw.contains("id") || !raw["id"].is_string() || raw["id"].get<std::string>().empty())
                    raw["id"] = response_id + "_item_" + std::to_string(index);
                if ((raw.value("type", "") == "function_call" ||
                     raw.value("type", "") == "custom_tool_call" ||
                     raw.value("type", "") == "tool_search_call") &&
                    (!raw.contains("call_id") || !raw["call_id"].is_string() || raw["call_id"].get<std::string>().empty()))
                    raw["call_id"] = fmt::generate_call_id();
                output.push_back(std::move(raw));
                ++index;
                continue;
            }
            json wire;
            wire["id"] = item.id.empty()
                ? response_id + "_item_" + std::to_string(index) : item.id;
            wire["status"] = item.status.empty() ? "completed" : item.status;
            if (!item.phase.empty()) wire["phase"] = item.phase;
            switch (item.item_kind) {
                case ItemKind::Message: {
                    wire["type"] = "message";
                    wire["role"] = item.role.empty() ? "assistant" : item.role;
                    json parts = json::array();
                    for (const auto &b : item.content)
                        if (b.kind == ContentKind::Text)
                            parts.push_back(json{{"type", "output_text"}, {"text", b.text}, {"annotations", json::array()}});
                    wire["content"] = std::move(parts);
                    break;
                }
                case ItemKind::Reasoning: {
                    wire["type"] = "reasoning";
                    json summary = json::array();
                    for (const auto &b : item.content)
                        if (b.kind == ContentKind::Thinking)
                            summary.push_back(json{{"type", "summary_text"}, {"text", b.text}});
                    wire["summary"] = std::move(summary);
                    if (!item.encrypted_content.empty()) wire["encrypted_content"] = item.encrypted_content;
                    break;
                }
                case ItemKind::FunctionCall:
                case ItemKind::CustomToolCall:
                case ItemKind::ToolSearchCall: {
                    wire["type"] = item.item_kind == ItemKind::CustomToolCall ? "custom_tool_call" :
                                    item.item_kind == ItemKind::ToolSearchCall ? "tool_search_call" : "function_call";
                    if (!item.call_id.empty()) wire["call_id"] = item.call_id;
                    else if (item.item_kind == ItemKind::FunctionCall ||
                             item.item_kind == ItemKind::CustomToolCall ||
                             item.item_kind == ItemKind::ToolSearchCall)
                        wire["call_id"] = fmt::generate_call_id();
                    if (!item.name.empty()) wire["name"] = item.name;
                    if (!item.namespace_name.empty()) wire["namespace"] = item.namespace_name;
                    if (item.item_kind == ItemKind::CustomToolCall) wire["input"] = item.payload;
                    else if (!item.payload.empty()) {
                        if (item.item_kind == ItemKind::ToolSearchCall) {
                            try { wire["arguments"] = json::parse(item.payload); }
                            catch (...) { wire["arguments"] = json::object(); }
                        } else {
                            // Responses function_call.arguments is a JSON
                            // string; preserve its original bytes.
                            wire["arguments"] = item.payload;
                        }
                    }
                    if (item.item_kind == ItemKind::ToolSearchCall) {
                        wire["execution"] = item.execution.is_null() ||
                            item.execution.empty() ? json("client") : item.execution;
                    } else if (!item.execution.is_null() && !item.execution.empty()) {
                        wire["execution"] = item.execution;
                    }
                    break;
                }
                case ItemKind::FunctionCallOutput:
                case ItemKind::CustomToolCallOutput:
                case ItemKind::ToolSearchOutput: {
                    wire["type"] = item.item_kind == ItemKind::CustomToolCallOutput ? "custom_tool_call_output" :
                                    item.item_kind == ItemKind::ToolSearchOutput ? "tool_search_output" : "function_call_output";
                    wire["call_id"] = item.call_id;
                    ContentBlock result;
                    result.kind = ContentKind::ToolResult;
                    result.nested = item.content;
                    result.text = fmt::tool_result_text(result);
                    wire["output"] = fmt::serialize_responses_tool_result_value(result);
                    break;
                }
                case ItemKind::Opaque:
                    if (item.extra.contains("raw_item")) wire = item.extra["raw_item"];
                    break;
            }
            output.push_back(std::move(wire));
            ++index;
        }
    }
    if (in.output_items.empty()) {
    json msg_parts = json::array();
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
                item["id"] = b.tool_call_id.empty()
                    ? response_id + "_item_" + std::to_string(output.size())
                    : b.tool_call_id;
                item["call_id"] = b.tool_call_id.empty() ? fmt::generate_call_id()
                                                          : b.tool_call_id;
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
        item["id"] = response_id + "_item_" + std::to_string(output.size());
        item["type"] = "message";
        item["status"] = "completed";
        item["role"] = "assistant";
        item["content"] = std::move(msg_parts);
        output.push_back(std::move(item));
    }
    if (!reasoning_items.empty()) {
        json item;
        item["id"] = response_id + "_item_" + std::to_string(output.size());
        item["type"] = "reasoning";
        item["summary"] = std::move(reasoning_items);
        output.push_back(std::move(item));
    }
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
