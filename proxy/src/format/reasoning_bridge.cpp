#include "reasoning_bridge.h"

#include <cstring>

namespace {

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

std::string summary_text(const json &item) {
    std::string text;
    if (!item.contains("summary") || !item["summary"].is_array()) return text;
    for (const auto &part : item["summary"])
        if (part.is_object() && part.contains("text") && part["text"].is_string())
            text += part["text"].get<std::string>();
    return text;
}

/// The fields an envelope may carry back to the upstream (audit 01-P-17 ①).
/// Upstream-only output fields (`status`, `object`, …) would be replayed as
/// unknown parameters, so they never enter the carrier.
json replayable_item(const json &item) {
    json out = json::object();
    for (const char *key : {"type", "id", "summary", "encrypted_content"})
        if (item.contains(key)) out[key] = item[key];
    return out;
}

bool is_openai_prefix(const char *prefix) {
    return std::strcmp(prefix, fmt::kOpenAIReasoningPrefix) == 0;
}

/// Decode `value` (which must start with `prefix`) into `decoded`.
///
/// The encoder is the validator: re-encoding what the payload decoded to has to
/// reproduce the carrier byte for byte.  For the OpenAI prefix the re-encode is
/// whitelisted, so a value carrying fields this service never writes does not
/// validate.  Carriers written before the whitelist existed hold the whole item;
/// they are still ours (they reproduce themselves) and are read with the
/// whitelist applied, so stored conversations keep working while no new carrier
/// can be that shape.
bool decode_and_validate(const std::string &value, const char *prefix,
                         json &decoded) {
    const std::string prefix_text(prefix);
    if (value.rfind(prefix_text, 0) != 0) return false;
    std::string payload;
    if (!b64url_decode(value.substr(prefix_text.size()), payload)) return false;
    json parsed;
    try { parsed = json::parse(payload); }
    catch (...) { return false; }
    if (!parsed.is_object()) return false;
    const bool openai = is_openai_prefix(prefix);
    if (openai) {
        const json filtered = replayable_item(parsed);
        const bool canonical = prefix_text + b64url_encode(filtered.dump()) == value;
        const bool pre_whitelist = prefix_text + b64url_encode(parsed.dump()) == value;
        if (!canonical && !pre_whitelist) return false;
        decoded = filtered;
        return true;
    }
    if (prefix_text + b64url_encode(parsed.dump()) != value) return false;
    decoded = parsed;
    return true;
}

}  // namespace

namespace fmt {

const char kOpenAIReasoningPrefix[] = "token-board-openai-reasoning-v1:";
const char kAnthropicThinkingPrefix[] = "token-board-anthropic-thinking-v1:";

bool is_reasoning_envelope(const std::string &value) {
    return value.rfind(kOpenAIReasoningPrefix, 0) == 0 ||
           value.rfind(kAnthropicThinkingPrefix, 0) == 0;
}

std::string encode_reasoning_envelope(const json &item, const char *prefix) {
    const json payload =
        is_openai_prefix(prefix) ? replayable_item(item) : item;
    return std::string(prefix) + b64url_encode(payload.dump());
}

bool decode_reasoning_envelope(const std::string &value, const char *prefix,
                               json &item) {
    json decoded;
    if (!decode_and_validate(value, prefix, decoded)) return false;
    if (is_openai_prefix(prefix) &&
        decoded.value("type", "") != "reasoning")
        return false;
    item = std::move(decoded);
    return true;
}

json anthropic_block_from_responses_reasoning(const json &item) {
    // ① An item that came back with our Anthropic envelope in
    // `encrypted_content` is a block captured off an Anthropic response: replay
    // it verbatim, vendor signature included, rather than re-wrapping an opaque
    // ciphertext (audit 01-P-17 ④).
    json captured;
    const std::string encrypted = item.value("encrypted_content", "");
    if (decode_reasoning_envelope(encrypted, kAnthropicThinkingPrefix, captured) &&
        (captured.value("type", "") == "thinking" ||
         captured.value("type", "") == "redacted_thinking"))
        return captured;

    // ② A Responses reasoning item cannot be given a signature Anthropic would
    // verify, so the value written there is an envelope this service owns.  The
    // block keeps the visible summary (possibly empty) and is never converted to
    // `redacted_thinking` (audit 01-P-17 ③).
    const std::string text = summary_text(item);
    if (!encrypted.empty()) {
        return json{{"type", "thinking"},
                    {"thinking", text},
                    {"signature", encode_reasoning_envelope(item,
                                                            kOpenAIReasoningPrefix)}};
    }
    if (!text.empty()) return json{{"type", "thinking"}, {"thinking", text}};
    // Neither carrier nor text: nothing to put on the wire.
    return json();
}

json responses_reasoning_from_anthropic_block(const json &block) {
    const std::string signed_value = block.value("type", "") == "redacted_thinking"
        ? block.value("data", "") : block.value("signature", "");
    json original;
    // Our own Responses item comes back exactly as it went out.
    if (decode_reasoning_envelope(signed_value, kOpenAIReasoningPrefix, original))
        return original;
    json item{{"type", "reasoning"}, {"summary", json::array()}};
    const std::string text = block.value("thinking", "");
    if (!text.empty())
        item["summary"].push_back({{"type", "summary_text"}, {"text", text}});
    if (!signed_value.empty())
        // A vendor-signed block cannot be reconstructed from its text alone:
        // keep the whole block in the opaque carrier so replaying it to the same
        // vendor restores the signature that vendor issued (audit 01-P-17 ④).
        item["encrypted_content"] =
            encode_reasoning_envelope(block, kAnthropicThinkingPrefix);
    return item;
}

void strip_reasoning_envelopes(json &body) {
    if (!body.is_object()) return;
    auto strip_blocks = [](json &blocks) {
        if (!blocks.is_array()) return;
        for (auto &block : blocks) {
            if (!block.is_object()) continue;
            const std::string type = block.value("type", "");
            if (type == "thinking" && block.contains("signature") &&
                block["signature"].is_string() &&
                is_reasoning_envelope(block["signature"].get<std::string>())) {
                // Keep the visible text, drop the carrier the upstream cannot
                // verify (audit 01-P-17 ⑤).
                block.erase("signature");
            } else if (type == "redacted_thinking" && block.contains("data") &&
                       block["data"].is_string() &&
                       is_reasoning_envelope(block["data"].get<std::string>())) {
                // The block had no visible text: keep the slot as an unsigned,
                // empty thinking block so the message stays structurally valid.
                block = json{{"type", "thinking"}, {"thinking", ""}};
            }
        }
    };
    if (body.contains("system")) strip_blocks(body["system"]);
    if (body.contains("messages") && body["messages"].is_array())
        for (auto &message : body["messages"])
            if (message.is_object() && message.contains("content"))
                strip_blocks(message["content"]);
}

}  // namespace fmt
