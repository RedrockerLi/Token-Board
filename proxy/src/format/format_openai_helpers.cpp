#include "format_openai_internal.h"

#include <array>

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

