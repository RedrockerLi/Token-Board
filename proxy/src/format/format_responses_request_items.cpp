#include "format_responses_internal.h"

using namespace ir;

json serialize_responses_input(const ir::ChatRequest &request,
                               const ir::ConversionContext *context) {
    json input = json::array();
    const auto &items = request.items.empty() ? request.messages : request.items;
    for (const auto &m : items) {
        if (m.extra.contains("raw_item") && m.extra["raw_item"].is_object()) {
            json raw = m.extra["raw_item"];
            if (context && raw.value("type", "") == "function_call") {
                for (const auto &mapping : context->tools.mappings) {
                    if (mapping.original_name != raw.value("name", "")) continue;
                    if (!mapping.namespace_name.empty() &&
                        raw.value("namespace", "") != mapping.namespace_name) continue;
                    raw["name"] = mapping.flat_name;
                    raw.erase("namespace");
                    if (mapping.kind == ToolKind::Custom) {
                        raw["type"] = "custom_tool_call";
                        try {
                            raw["input"] = json::parse(raw.value("arguments", ""))
                                .value("input", "");
                            raw.erase("arguments");
                        } catch (...) {}
                    } else if (mapping.kind == ToolKind::ToolSearch) {
                        raw["type"] = "tool_search_call";
                        if (raw.contains("arguments") && raw["arguments"].is_string()) {
                            try { raw["arguments"] = json::parse(raw["arguments"].get<std::string>()); }
                            catch (...) {}
                        }
                    }
                    break;
                }
            }
            input.push_back(std::move(raw));
            continue;
        }

        std::vector<ContentBlock> message_content;
        json reasoning_summary = json::array();
        auto flush_message = [&] {
            if (message_content.empty()) return;
            input.push_back(json{{"type", "message"}, {"role", m.role},
                                 {"content", serialize_responses_content(
                                     message_content, false)}});
            message_content.clear();
        };
        auto flush_reasoning = [&] {
            if (reasoning_summary.empty()) return;
            input.push_back(json{{"type", "reasoning"},
                                 {"summary", std::move(reasoning_summary)}});
            reasoning_summary = json::array();
        };
        for (const auto &block : m.content) {
            if (block.kind == ContentKind::Thinking) {
                flush_message();
                if (block.extra.contains("responses_reasoning_item") &&
                    block.extra["responses_reasoning_item"].is_object()) {
                    flush_reasoning();
                    input.push_back(block.extra["responses_reasoning_item"]);
                } else {
                    reasoning_summary.push_back(
                        json{{"type", "summary_text"}, {"text", block.text}});
                }
                continue;
            }
            flush_reasoning();
            if (block.kind == ContentKind::ToolResult) {
                flush_message();
                input.push_back(json{
                    {"type", block.extra.value("type", "") == "custom"
                        ? "custom_tool_call_output" : "function_call_output"},
                    {"call_id", block.tool_use_id},
                    {"output", fmt::serialize_responses_tool_result_value(block)}});
            } else if (block.kind == ContentKind::ToolUse) {
                flush_message();
                json item{
                    {"type", block.extra.value("type", "") == "custom"
                        ? "custom_tool_call" : "function_call"},
                    {"call_id", block.tool_call_id}, {"name", block.tool_name}};
                if (block.extra.value("type", "") == "custom")
                    item["input"] = block.tool_input.value("input", "");
                else {
                    item["arguments"] = block.extra.value("raw_arguments", "");
                    if (item["arguments"].get<std::string>().empty())
                        item["arguments"] = block.tool_input.dump();
                }
                input.push_back(std::move(item));
            } else {
                message_content.push_back(block);
            }
        }
        flush_reasoning();
        flush_message();
    }
    return input;
}
