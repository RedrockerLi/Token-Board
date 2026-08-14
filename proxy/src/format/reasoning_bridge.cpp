#include "reasoning_bridge.h"

#include <cctype>
#include <cstring>

namespace {
const char kOpenAI[] = "token-board-openai-reasoning-v1:";
const char kAnthropic[] = "token-board-anthropic-thinking-v1:";

std::string b64url_encode(const std::string &in) {
    static const char table[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    std::string out;
    unsigned int value = 0; int bits = -6;
    for (unsigned char c : in) {
        value = (value << 8) | c; bits += 8;
        while (bits >= 0) { out.push_back(table[(value >> bits) & 0x3f]); bits -= 6; }
    }
    if (bits > -6) out.push_back(table[((value << 8) >> (bits + 8)) & 0x3f]);
    return out;
}

bool b64url_decode(const std::string &in, std::string &out) {
    static const std::string table =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    unsigned int value = 0; int bits = -8;
    out.clear();
    for (unsigned char c : in) {
        const auto pos = table.find(c);
        if (pos == std::string::npos) return false;
        value = (value << 6) | static_cast<unsigned int>(pos); bits += 6;
        if (bits >= 0) { out.push_back(static_cast<char>((value >> bits) & 0xff)); bits -= 8; }
    }
    return true;
}

std::string summary(const json &item) {
    std::string text;
    if (!item.contains("summary") || !item["summary"].is_array()) return text;
    for (const auto &part : item["summary"])
        if (part.is_object() && part.contains("text") && part["text"].is_string())
            text += part["text"].get<std::string>();
    return text;
}
}

namespace fmt {
std::string encode_reasoning_envelope(const json &item, const char *prefix) {
    return std::string(prefix) + b64url_encode(item.dump());
}

bool decode_reasoning_envelope(const std::string &value, const char *prefix,
                               json &item) {
    if (value.rfind(prefix, 0) != 0) return false;
    std::string decoded;
    if (!b64url_decode(value.substr(std::strlen(prefix)), decoded)) return false;
    try { item = json::parse(decoded); }
    catch (...) { return false; }
    return item.is_object() && item.value("type", "") == "reasoning";
}

json anthropic_block_from_responses_reasoning(const json &item) {
    const std::string text = summary(item);
    const std::string envelope = encode_reasoning_envelope(item, kOpenAI);
    if (text.empty() && !item.value("encrypted_content", "").empty())
        return json{{"type", "redacted_thinking"}, {"data", envelope}};
    // Even summary-only Responses reasoning carries id/status/phase/extra in
    // the envelope, so an Anthropic round-trip does not discard metadata.
    return json{{"type", "thinking"}, {"thinking", text}, {"signature", envelope}};
}

json responses_reasoning_from_anthropic_block(const json &block) {
    json original;
    const std::string signed_value = block.value("type", "") == "redacted_thinking"
        ? block.value("data", "") : block.value("signature", "");
    if (decode_reasoning_envelope(signed_value, kOpenAI, original)) return original;
    json item{{"type", "reasoning"}, {"summary", json::array()}};
    const std::string text = block.value("thinking", "");
    if (!text.empty()) item["summary"].push_back({{"type", "summary_text"}, {"text", text}});
    if (!signed_value.empty()) item["encrypted_content"] = encode_reasoning_envelope(block, kAnthropic);
    return item;
}
}
