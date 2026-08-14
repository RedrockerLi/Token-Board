#include "format_openai_internal.h"

#include <array>

void append_openai_content_part(json &parts, json &tool_calls, bool &has_tools,
                                std::string &reasoning_text,
                                const ir::ContentBlock &b) {
    using namespace ir;
    switch (b.kind) {
        case ContentKind::Text:
            if (b.extra.contains("raw") && b.extra["raw"].is_object())
                parts.push_back(b.extra["raw"]);
            else
                parts.push_back(json{{"type", "text"}, {"text", b.text}});
            break;
        case ContentKind::Image:
            parts.push_back(fmt::image_block_to_openai_part(b));
            break;
        case ContentKind::File:
            parts.push_back(fmt::serialize_openai_file_part(b));
            break;
        case ContentKind::Audio:
            parts.push_back(fmt::serialize_openai_audio_part(b));
            break;
        case ContentKind::Thinking:
            if (b.extra.contains("raw") && b.extra["raw"].is_object())
                parts.push_back(b.extra["raw"]);
            else
                reasoning_text += b.text;
            break;
        case ContentKind::ToolUse: {
            has_tools = true;
            json tc{{"type", "function"},
                    {"function", json{{"name", b.tool_name},
                                       {"arguments", b.extra.value("raw_arguments", "")}}}};
            if (tc["function"]["arguments"].get<std::string>().empty())
                tc["function"]["arguments"] = b.tool_input.dump();
            if (!b.tool_call_id.empty()) tc["id"] = b.tool_call_id;
            tool_calls.push_back(std::move(tc));
            break;
        }
        default:
            break;
    }
}

bool openai_request_key_consumed(const std::string &key) {
    static constexpr std::array<const char *, 14> consumed{{
        "model", "messages", "system", "tools", "tool_choice", "stream",
        "reasoning_effort", "reasoning", "max_tokens",
        "max_completion_tokens", "temperature", "stop", "stream_options",
        "metadata",
    }};
    for (const char *item : consumed)
        if (key == item) return true;
    return false;
}

void parse_openai_namespace_children(const json &value, ir::Tool &tool) {
    const auto &children = value.contains("tools") ? value["tools"]
        : value.value("children", json::array());
    if (!children.is_array()) return;
    for (const auto &child : children) {
        if (!child.is_object()) continue;
        ir::Tool nested;
        nested.wire_type = child.value("type", "function");
        nested.kind = nested.wire_type == "custom" ? ir::ToolKind::Custom
            : nested.wire_type == "function" ? ir::ToolKind::Function
            : ir::ToolKind::Hosted;
        nested.name = child.value("name", "");
        nested.description = child.value("description", "");
        if (child.contains("parameters")) nested.input_schema = child["parameters"];
        nested.raw = child; nested.extra["raw"] = child;
        tool.children.push_back(std::move(nested));
    }
}

void collapse_openai_system_messages(json &messages) {
    if (!messages.is_array()) return;
    std::vector<std::string> system;
    json rest = json::array();
    for (auto &message : messages) {
        if (message.is_object() && message.value("role", "") == "system") {
            const auto &content = message["content"];
            const std::string text = content.is_string()
                ? content.get<std::string>() : content.dump();
            if (!text.empty()) system.push_back(text);
        } else {
            rest.push_back(std::move(message));
        }
    }
    messages = json::array();
    if (!system.empty()) {
        std::string joined;
        for (std::size_t index = 0; index < system.size(); ++index) {
            if (index) joined += "\n\n";
            joined += system[index];
        }
        messages.push_back({{"role", "system"}, {"content", joined}});
    }
    for (auto &message : rest) messages.push_back(std::move(message));
}
