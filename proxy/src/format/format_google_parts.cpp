#include "format_google_internal.h"

namespace gfmt {
namespace {

/// Documented escape hatch for a replayed function call whose opaque signature
/// was not carried by the source protocol.
constexpr const char *kSignatureSentinel = "skip_thought_signature_validator";

std::string image_mime(const ir::ContentBlock &block) {
    return block.media_type.empty() ? "image/png" : block.media_type;
}

std::string audio_mime(const ir::ContentBlock &block) {
    if (!block.media_type.empty()) return block.media_type;
    return block.audio_format.empty() ? "audio/mp3"
                                      : "audio/" + block.audio_format;
}

std::string file_mime(const ir::ContentBlock &block) {
    return block.media_type.empty() ? "application/octet-stream"
                                    : block.media_type;
}

/// Media block → a Gemini part that carries bytes inline or a file URI.
/// Returns a discarded json when the block cannot be expressed on this wire.
json media_part(const ir::ContentBlock &block) {
    switch (block.kind) {
        case ir::ContentKind::Image:
            if (!block.image_data_b64.empty())
                return json{{"inlineData", {{"mimeType", image_mime(block)},
                                            {"data", block.image_data_b64}}}};
            // Gemini fetches only Google-hosted URIs, never arbitrary URLs;
            // the request capability check rejects such input before this.
            if (block.file_url.empty()) return json();
            return json{{"fileData", {{"mimeType", image_mime(block)},
                                      {"fileUri", block.file_url}}}};
        case ir::ContentKind::Audio:
            if (block.audio_data_b64.empty()) return json();
            return json{{"inlineData", {{"mimeType", audio_mime(block)},
                                        {"data", block.audio_data_b64}}}};
        case ir::ContentKind::File:
            if (!block.file_data_b64.empty())
                return json{{"inlineData", {{"mimeType", file_mime(block)},
                                            {"data", block.file_data_b64}}}};
            if (!block.file_url.empty())
                return json{{"fileData", {{"mimeType", file_mime(block)},
                                          {"fileUri", block.file_url}}}};
            return json();  // provider-managed file ids do not exist here
        default:
            return json();
    }
}

json function_response_part(const ir::ContentBlock &block) {
    json media = json::array();
    for (const auto &nested : block.nested) {
        if (nested.kind == ir::ContentKind::Text ||
            nested.kind == ir::ContentKind::Thinking)
            continue;
        json part = media_part(nested);
        if (!part.is_null() && !part.empty()) media.push_back(std::move(part));
    }
    const std::string text = fmt::tool_result_text(block);
    json response;
    response["name"] = block.tool_name;
    response["content"] = text;
    const bool is_error = block.extra.contains("is_error") &&
        block.extra["is_error"].is_boolean() &&
        block.extra["is_error"].get<bool>();
    if (is_error) response["error"] = text;
    json call;
    call["name"] = block.tool_name;
    call["response"] = std::move(response);
    if (!block.tool_use_id.empty()) call["id"] = block.tool_use_id;
    if (!media.empty()) call["parts"] = std::move(media);
    return json{{"functionResponse", std::move(call)}};
}

void blocks_from_function_response(const json &call,
                                   std::vector<ir::ContentBlock> &output) {
    ir::ContentBlock block;
    block.kind = ir::ContentKind::ToolResult;
    block.tool_name = call.value("name", "");
    if (call.contains("id") && call["id"].is_string())
        block.tool_use_id = call["id"].get<std::string>();
    if (call.contains("parts") && call["parts"].is_array())
        blocks_from_parts(call["parts"], block.nested);
    std::string text;
    if (call.contains("response")) {
        const json &response = call["response"];
        if (response.is_string()) {
            text = response.get<std::string>();
        } else if (response.is_object() && response.contains("content") &&
                   response["content"].is_string()) {
            text = response["content"].get<std::string>();
        } else if (!response.is_null()) {
            text = response.dump();
        }
        if (response.is_object() &&
            response.contains("error") && response["error"].is_string()) {
            block.extra["is_error"] = true;
            if (text.empty()) text = response["error"].get<std::string>();
        }
    }
    if (!text.empty() || block.nested.empty()) {
        ir::ContentBlock text_block;
        text_block.kind = ir::ContentKind::Text;
        text_block.text = std::move(text);
        block.nested.insert(block.nested.begin(), std::move(text_block));
    }
    output.push_back(std::move(block));
}

}  // namespace

json parts_from_blocks(const std::vector<ir::ContentBlock> &blocks,
                       bool require_signature) {
    json parts = json::array();
    for (const auto &block : blocks) {
        // A block parsed from this wire that has no IR representation is
        // replayed verbatim instead of being silently degraded.
        if (block.extra.contains("raw") && block.extra["raw"].is_object()) {
            parts.push_back(block.extra["raw"]);
            continue;
        }
        switch (block.kind) {
            case ir::ContentKind::Text: {
                json part;
                part["text"] = block.text;
                if (block.extra.contains("thoughtSignature") &&
                    block.extra["thoughtSignature"].is_string())
                    part["thoughtSignature"] = block.extra["thoughtSignature"];
                parts.push_back(std::move(part));
                break;
            }
            case ir::ContentKind::Thinking: {
                json part;
                if (block.extra.contains("inline_data") &&
                    block.extra["inline_data"].is_object()) {
                    part["inlineData"] = block.extra["inline_data"];
                } else {
                    part["text"] = block.text;
                }
                part["thought"] = true;
                if (block.extra.contains("thoughtSignature") &&
                    block.extra["thoughtSignature"].is_string())
                    part["thoughtSignature"] = block.extra["thoughtSignature"];
                parts.push_back(std::move(part));
                break;
            }
            case ir::ContentKind::ToolUse: {
                json call;
                if (!block.tool_call_id.empty()) call["id"] = block.tool_call_id;
                call["name"] = block.tool_name;
                call["args"] = block.tool_input.is_object()
                    ? block.tool_input : json::object();
                json part;
                part["functionCall"] = std::move(call);
                if (block.extra.contains("thoughtSignature") &&
                    block.extra["thoughtSignature"].is_string())
                    part["thoughtSignature"] = block.extra["thoughtSignature"];
                else if (require_signature)
                    part["thoughtSignature"] = kSignatureSentinel;
                parts.push_back(std::move(part));
                break;
            }
            case ir::ContentKind::ToolResult:
                parts.push_back(function_response_part(block));
                break;
            default: {
                json part = media_part(block);
                if (part.is_null() || part.empty()) break;
                parts.push_back(std::move(part));
                break;
            }
        }
    }
    return parts;
}

void blocks_from_parts(const json &parts, std::vector<ir::ContentBlock> &out) {
    if (!parts.is_array()) return;
    for (const auto &part : parts) {
        if (!part.is_object()) continue;
        const bool thought = part.value("thought", false);
        if (part.contains("functionCall") && part["functionCall"].is_object()) {
            const json &call = part["functionCall"];
            ir::ContentBlock block;
            block.kind = ir::ContentKind::ToolUse;
            block.tool_name = call.value("name", "");
            if (call.contains("id") && call["id"].is_string())
                block.tool_call_id = call["id"].get<std::string>();
            // Gemini omits ids on most models; a stable synthetic id keeps the
            // tool result matchable when the conversation is replayed.
            if (block.tool_call_id.empty())
                block.tool_call_id = fmt::generate_call_id();
            if (call.contains("args") && call["args"].is_object())
                block.tool_input = call["args"];
            if (part.contains("thoughtSignature") &&
                part["thoughtSignature"].is_string())
                block.extra["thoughtSignature"] = part["thoughtSignature"];
            out.push_back(std::move(block));
        } else if (part.contains("functionResponse") &&
                   part["functionResponse"].is_object()) {
            blocks_from_function_response(part["functionResponse"], out);
        } else if (part.contains("inlineData") &&
                   part["inlineData"].is_object()) {
            const json &inline_data = part["inlineData"];
            const std::string mime = inline_data.value("mimeType", "");
            const std::string data = inline_data.value("data", "");
            ir::ContentBlock block;
            if (thought) {
                block.kind = ir::ContentKind::Thinking;
                block.extra["inline_data"] = inline_data;
            } else if (mime.rfind("image/", 0) == 0) {
                block.kind = ir::ContentKind::Image;
                block.media_type = mime;
                block.image_data_b64 = data;
            } else if (mime.rfind("audio/", 0) == 0) {
                block.kind = ir::ContentKind::Audio;
                block.media_type = mime;
                block.audio_data_b64 = data;
                block.audio_format = mime.substr(6);
            } else {
                block.kind = ir::ContentKind::File;
                block.media_type = mime;
                block.file_data_b64 = data;
            }
            if (part.contains("thoughtSignature") &&
                part["thoughtSignature"].is_string())
                block.extra["thoughtSignature"] = part["thoughtSignature"];
            out.push_back(std::move(block));
        } else if (part.contains("fileData") && part["fileData"].is_object()) {
            const json &file_data = part["fileData"];
            ir::ContentBlock block;
            block.kind = ir::ContentKind::File;
            block.file_url = file_data.value("fileUri", "");
            block.media_type = file_data.value("mimeType", "");
            out.push_back(std::move(block));
        } else if (part.contains("text") && part["text"].is_string()) {
            ir::ContentBlock block;
            block.kind = thought ? ir::ContentKind::Thinking
                                 : ir::ContentKind::Text;
            block.text = part["text"].get<std::string>();
            if (thought) block.extra["thought"] = true;
            if (part.contains("thoughtSignature") &&
                part["thoughtSignature"].is_string())
                block.extra["thoughtSignature"] = part["thoughtSignature"];
            out.push_back(std::move(block));
        } else {
            // executableCode / codeExecutionResult / videoMetadata / future
            // part kinds have no IR representation: keep them raw so a
            // same-format replay is lossless and a cross-format conversion is
            // refused instead of silently dropping content.
            ir::ContentBlock block;
            block.kind = ir::ContentKind::Text;
            block.extra["raw"] = part;
            out.push_back(std::move(block));
        }
    }
}

}  // namespace gfmt
