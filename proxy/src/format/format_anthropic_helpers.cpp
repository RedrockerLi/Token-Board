#include "format_anthropic_internal.h"
#include "reasoning_bridge.h"

#include <array>
#include <atomic>
#include <cstdint>

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
    // The budget must grow with the effort tier, so that a weaker tier can
    // never buy more thinking than a stronger one (audit 01-P-03: `minimal`
    // used to fall through to 2048, i.e. twice `low`).  1024 is Anthropic's
    // documented floor for `thinking.budget_tokens`, so `minimal` sits on that
    // floor instead of going below it and being rejected.
    if (effort == "minimal") return 1024;
    if (effort == "low") return 2048;
    if (effort == "medium" || effort.empty()) return 4096;
    if (effort == "high") return 8192;
    if (effort == "xhigh") return 16384;
    // An unknown tier is not guessed from its spelling: fall back to `medium`
    // (the historical default) and say so, first occurrence and every
    // thousandth after it — the same sampling the other informational paths in
    // this file use.
    static std::atomic<std::uint64_t> unknown_effort{0};
    const std::uint64_t occurrence =
        unknown_effort.fetch_add(1, std::memory_order_relaxed) + 1;
    if (occurrence == 1 || occurrence % 1000 == 0)
        TB_LOG_WARN("[Anthropic] unknown reasoning effort \"%s\", using the "
                    "medium thinking budget (occurrence %llu)\n",
                    effort.c_str(),
                    static_cast<unsigned long long>(occurrence));
    return 4096;
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
        } else if (type == "document") {
            fmt::parse_media_content(block, output, false);
            continue;
        } else if (type == "audio" || type == "input_audio") {
            fmt::parse_media_content(block, output, false);
            continue;
        } else if (type == "tool_use") {
            item.kind = ContentKind::ToolUse;
            item.tool_call_id = block.value("id", "");
            item.tool_name = block.value("name", "");
            if (block.contains("input")) item.tool_input = block["input"];
        } else if (type == "tool_result") {
            item.kind = ContentKind::ToolResult;
            item.tool_use_id = block.value("tool_use_id", "");
            if (block.contains("is_error")) item.extra["is_error"] = block["is_error"];
            const json content = block.value("content", json());
            fmt::parse_tool_result_content(content, item);
        } else if (type == "thinking" || type == "redacted_thinking") {
            item.kind = ContentKind::Thinking;
            item.text = block.value("thinking", "");
            if (block.contains("signature")) item.extra["signature"] = block["signature"];
            if (type == "redacted_thinking") {
                item.extra["redacted"] = true;
                if (block.contains("data")) item.extra["data"] = block["data"];
            }
            item.extra["responses_reasoning_item"] =
                fmt::responses_reasoning_from_anthropic_block(block);
        } else {
            // Preserve provider extensions and future block types as opaque
            // content.  The capability preflight can then reject a
            // cross-format conversion instead of silently deleting input;
            // an Anthropic-to-Anthropic rewrite still remains lossless.
            item.kind = ContentKind::Text;
            item.extra["raw"] = block;
        }
        output.push_back(std::move(item));
    }
}
