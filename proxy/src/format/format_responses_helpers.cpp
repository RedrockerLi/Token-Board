#include "format_responses_internal.h"

#include <array>

using namespace ir;

bool responses_request_key_consumed(const std::string &key) {
    // Keep this list aligned with the public Responses create parameters.
    // Unknown provider extensions are still retained in extras, but known
    // fields must survive a Responses→Responses codec round-trip.
    static constexpr std::array<const char *, 31> consumed{{
        "background", "context_management", "conversation", "include",
        "input", "instructions", "max_output_tokens", "max_tool_calls",
        "metadata", "model", "moderation", "parallel_tool_calls",
        "previous_response_id", "prompt", "prompt_cache_key",
        "prompt_cache_options", "prompt_cache_retention", "reasoning",
        "safety_identifier", "service_tier", "store", "stream",
        "stream_options", "temperature", "text", "tool_choice", "tools",
        "top_logprobs", "top_p", "truncation", "user",
    }};
    for (const char *item : consumed)
        if (key == item) return true;
    return false;
}

json parse_responses_arguments(const std::string &arguments) {
    try {
        json value = json::parse(arguments);
        return value.is_object() ? value : json::object();
    } catch (...) {
        return json::object();
    }
}

void parse_responses_content(const json &content,
                             std::vector<ContentBlock> &output) {
    if (content.is_string()) {
        ContentBlock block;
        block.kind = ContentKind::Text;
        block.text = content.get<std::string>();
        output.push_back(std::move(block));
        return;
    }
    if (!content.is_array()) return;
    for (const auto &part : content) {
        if (!part.is_object()) continue;
        ContentBlock block;
        const std::string type = part.value("type", "");
        if (type == "input_text" || type == "output_text") {
            block.kind = ContentKind::Text;
            block.text = part.value("text", "");
        } else if (type == "input_image") {
            block.kind = ContentKind::Image;
            const auto &url = part.contains("image_url")
                ? part["image_url"] : json();
            if (url.is_string()) block.image_url = url.get<std::string>();
            else if (url.is_object()) block.image_url = url.value("url", "");
            if (part.contains("detail")) block.extra["detail"] = part["detail"];
            std::string media, encoded;
            if (fmt::parse_data_uri(block.image_url, media, encoded)) {
                block.media_type = std::move(media);
                block.image_data_b64 = std::move(encoded);
                block.image_url.clear();
            }
        } else if (type == "input_file" || type == "input_audio" ||
                   type == "file" || type == "audio") {
            fmt::parse_media_content(part, output, false);
            continue;
        } else {
            // File/audio/computer-use parts have no lossless equivalent in
            // the common IR. Preserve the native Responses object so a
            // same-format rewrite does not silently discard user input.
            block.kind = ContentKind::Text;
            block.extra["raw"] = part;
        }
        output.push_back(std::move(block));
    }
}

json serialize_responses_content(const std::vector<ContentBlock> &blocks,
                                 bool output_style) {
    json result = json::array();
    for (const auto &block : blocks) {
        if (block.kind == ContentKind::Text) {
            if (block.extra.contains("raw") && block.extra["raw"].is_object())
                result.push_back(block.extra["raw"]);
            else result.push_back({{"type", output_style ? "output_text" : "input_text"},
                                   {"text", block.text}});
        } else if (block.kind == ContentKind::Image) {
            json value{{"type", "input_image"}};
            value["image_url"] = !block.image_url.empty()
                ? block.image_url
                : fmt::build_data_uri(block.media_type, block.image_data_b64);
            if (block.extra.contains("detail")) value["detail"] = block.extra["detail"];
            result.push_back(std::move(value));
        } else if (block.kind == ContentKind::File) {
            result.push_back(fmt::serialize_responses_file_part(block));
        } else if (block.kind == ContentKind::Audio) {
            result.push_back(fmt::serialize_responses_audio_part(block));
        }
    }
    return result;
}
