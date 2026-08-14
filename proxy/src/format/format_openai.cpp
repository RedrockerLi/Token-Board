#include "format_openai_internal.h"

#include <cstring>

#include "format_common.h"

using namespace ir;

namespace {

// Keys consumed (mapped to IR fields) — the rest land in IR extras.
const char *kConsumed[] = {
    "model", "messages", "system", "tools", "tool_choice", "stream",
    "reasoning_effort", "reasoning", "max_tokens", "max_completion_tokens",
    "temperature", "stop",
};

// MiniMax requires `role=system` only in the first slot; DeepSeek/other
// strict OpenAI layers reject mid-conversation system messages. Codex emits
// a system message from `instructions` plus one `developer` message per turn
// (mapped to `system` above), so merge them all into a single leading
// `system` message. Semantically lossless for lenient upstreams too —
// mirroring cc-switch's `collapse_system_messages_to_head`.
void collapse_system_messages_to_head(json &messages) {
    if (!messages.is_array()) return;
    std::vector<std::string> sys_texts;
    json rest = json::array();
    for (auto &m : messages) {
        if (m.is_object() && m.value("role", "") == "system") {
            std::string t;
            if (m.contains("content")) {
                const json &c = m["content"];
                t = c.is_string() ? c.get<std::string>() : c.dump();
            }
            if (!t.empty()) sys_texts.push_back(std::move(t));
        } else {
            rest.push_back(std::move(m));
        }
    }
    messages = json::array();
    if (!sys_texts.empty()) {
        std::string joined;
        for (size_t i = 0; i < sys_texts.size(); ++i) {
            if (i) joined += "\n\n";
            joined += sys_texts[i];
        }
        messages.push_back(json{{"role", "system"}, {"content", joined}});
    }
    for (auto &m : rest) messages.push_back(std::move(m));
}

// Extra keys this format can forward through (not regenerated).

}  // namespace

std::unique_ptr<ir::StreamParser> OpenAICodec::make_stream_parser(
    const ir::ConversionContext *context) const {
    return make_openai_stream_parser_impl(context);
}
std::unique_ptr<ir::StreamEmitter> OpenAICodec::make_stream_emitter(const ir::ConversionContext *) const {
    return make_openai_stream_emitter_impl();
}

std::unique_ptr<FormatCodec> make_openai_codec() {
    return std::make_unique<OpenAICodec>();
}
