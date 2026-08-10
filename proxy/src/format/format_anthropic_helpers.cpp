#include "format_anthropic_internal.h"

#include <array>

using namespace ir;

bool anthropic_request_key_consumed(const std::string &key) {
    static constexpr std::array<const char *, 14> consumed{{
        "model", "system", "messages", "tools", "tool_choice",
        "max_tokens", "temperature", "stop_sequences", "thinking", "stream",
        "top_p", "top_k", "metadata", "service_tier",
    }};
    for (const char *item : consumed)
        if (key == item) return true;
    return false;
}

int anthropic_effort_budget(const std::string &effort) {
    if (effort == "low") return 1024;
    if (effort == "high") return 4096;
    return 2048;
}

void parse_anthropic_blocks(const json &value,
                            std::vector<ContentBlock> &output) {
    for (const auto &block : value) {
        if (!block.is_object()) continue;
        ContentBlock item;
        const std::string type = block.value("type", "");
        if (type == "text") {
            item.kind = ContentKind::Text;
            item.text = block.value("text", "");
            if (block.contains("cache_control"))
                item.extra["cache_control"] = block["cache_control"];
        } else if (type == "image") {
            item.kind = ContentKind::Image;
            const json source = block.value("source", json::object());
            if (source.value("type", "") == "base64") {
                item.image_data_b64 = source.value("data", "");
                item.media_type = source.value("media_type", "");
            } else item.image_url = source.value("url", "");
        } else if (type == "tool_use") {
            item.kind = ContentKind::ToolUse;
            item.tool_call_id = block.value("id", "");
            item.tool_name = block.value("name", "");
            if (block.contains("input")) item.tool_input = block["input"];
        } else if (type == "tool_result") {
            item.kind = ContentKind::ToolResult;
            item.tool_use_id = block.value("tool_use_id", "");
            const json content = block.value("content", json());
            if (content.is_string()) item.text = content.get<std::string>();
            else if (content.is_array())
                for (const auto &part : content)
                    if (part.is_object() && part.value("type", "") == "text")
                        item.text += part.value("text", "");
        } else if (type == "thinking" || type == "redacted_thinking") {
            item.kind = ContentKind::Thinking;
            item.text = block.value("thinking", "");
            if (block.contains("signature")) item.extra["signature"] = block["signature"];
            if (type == "redacted_thinking") {
                item.extra["redacted"] = true;
                if (block.contains("data")) item.extra["data"] = block["data"];
            }
        } else continue;
        output.push_back(std::move(item));
    }
}

