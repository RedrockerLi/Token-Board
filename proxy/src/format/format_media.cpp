#include "format_media.h"

#include <algorithm>
#include <cctype>

namespace fmt {
namespace {

constexpr int kMaxMediaDepth = 32;

bool is_image_type(const std::string &media) {
    return media.size() >= 6 && media.compare(0, 6, "image/") == 0;
}

bool has_raw_blocks(const std::vector<ir::ContentBlock> &blocks) {
    for (const auto &block : blocks) {
        if (block.extra.contains("raw") && block.extra["raw"].is_object())
            return true;
        if (has_raw_blocks(block.nested)) return true;
    }
    return false;
}

bool looks_base64ish(const std::string &value) {
    if (value.size() < 16 * 1024) return false;
    size_t valid = 0;
    for (unsigned char c : value) {
        if (std::isalnum(c) || c == '+' || c == '/' || c == '=' ||
            c == '\n' || c == '\r' || c == ' ' || c == '\t') valid++;
    }
    return valid * 100 >= value.size() * 98;
}

void clamp_residual_text(std::vector<ir::ContentBlock> &blocks) {
    for (auto &b : blocks) {
        if (b.kind == ir::ContentKind::Text &&
            ((b.text.rfind("data:", 0) == 0 && b.text.size() >= 8 * 1024) ||
             looks_base64ish(b.text)))
            b.text = "[large media payload omitted]";
        clamp_residual_text(b.nested);
    }
}

void image_from_url(const std::string &url, ir::ContentBlock &b) {
    b.kind = ir::ContentKind::Image;
    b.image_url = url;
    std::string media, data;
    if (parse_data_uri(url, media, data) && is_image_type(media)) {
        b.media_type = std::move(media);
        b.image_data_b64 = std::move(data);
        b.image_url.clear();
    }
}

void parse_media_content_impl(const json &value,
                              std::vector<ir::ContentBlock> &output,
                              bool parse_json_strings, int depth);

bool append_object(const json &v, std::vector<ir::ContentBlock> &out,
                   bool parse_json_strings, int depth);

void append_string(const std::string &s, std::vector<ir::ContentBlock> &out,
                   bool parse_json_strings, int depth) {
    std::string media, data;
    if (parse_data_uri(s, media, data) && is_image_type(media)) {
        ir::ContentBlock b;
        b.kind = ir::ContentKind::Image;
        b.media_type = std::move(media);
        b.image_data_b64 = std::move(data);
        out.push_back(std::move(b));
        return;
    }
    if (parse_json_strings && s.size() >= 2 &&
        (s.front() == '[' || s.front() == '{')) {
        try {
            json parsed = json::parse(s);
            std::vector<ir::ContentBlock> nested;
            if (parsed.is_array()) {
                for (const auto &item : parsed)
                    append_object(item, nested, false, depth + 1);
            } else if (parsed.is_object()) {
                append_object(parsed, nested, false, depth + 1);
            }
            bool media_found = false;
            for (const auto &b : nested)
                media_found = media_found || b.kind == ir::ContentKind::Image ||
                              b.kind == ir::ContentKind::File ||
                              b.kind == ir::ContentKind::Audio;
            if (media_found) {
                out.insert(out.end(), nested.begin(), nested.end());
                return;
            }
        } catch (...) {
        }
    }
    ir::ContentBlock b;
    b.kind = ir::ContentKind::Text;
    b.text = s;
    out.push_back(std::move(b));
}

void append_file(const json &v, std::vector<ir::ContentBlock> &out) {
    const json *src = &v;
    if (v.contains("file") && v["file"].is_object()) src = &v["file"];
    ir::ContentBlock b;
    b.kind = ir::ContentKind::File;
    b.file_id = src->value("file_id", "");
    b.file_url = src->value("file_url", "");
    if (b.file_url.empty()) b.file_url = src->value("url", "");
    b.filename = src->value("filename", "");
    std::string media, data = src->value("file_data", "");
    if (data.empty()) data = src->value("data", "");
    if (parse_data_uri(data, media, data)) b.media_type = std::move(media);
    b.file_data_b64 = std::move(data);
    if (v.contains("source") && v["source"].is_object()) {
        const auto &source = v["source"];
        b.file_url = source.value("url", b.file_url);
        b.filename = v.value("title", b.filename);
        std::string source_data = source.value("data", "");
        if (!source_data.empty()) b.file_data_b64 = std::move(source_data);
        b.media_type = source.value("media_type", b.media_type);
    }
    out.push_back(std::move(b));
}

void append_audio(const json &v, std::vector<ir::ContentBlock> &out) {
    const json *src = &v;
    if (v.contains("input_audio") && v["input_audio"].is_object())
        src = &v["input_audio"];
    ir::ContentBlock b;
    b.kind = ir::ContentKind::Audio;
    b.audio_data_b64 = src->value("data", "");
    b.audio_format = src->value("format", "");
    out.push_back(std::move(b));
}

bool append_object(const json &v, std::vector<ir::ContentBlock> &out,
                   bool parse_json_strings, int depth) {
    if (depth > kMaxMediaDepth) {
        ir::ContentBlock raw;
        raw.kind = ir::ContentKind::Text;
        raw.extra["raw"] = v;
        out.push_back(std::move(raw));
        return false;
    }
    if (!v.is_object()) {
        if (v.is_string()) append_string(v.get<std::string>(), out,
                                         parse_json_strings, depth);
        else if (!v.is_null()) append_string(v.dump(), out, false, depth);
        return false;
    }
    const std::string type = v.value("type", "");
    if (type == "text" || type == "input_text" || type == "output_text") {
        append_string(v.value("text", ""), out, false, depth);
        return false;
    }
    if (type == "image_url") {
        const json &url = v.contains("image_url") ? v["image_url"] : v;
        ir::ContentBlock b;
        image_from_url(url.is_string() ? url.get<std::string>()
                                       : url.value("url", ""), b);
        if (url.is_object() && url.contains("detail")) b.extra["detail"] = url["detail"];
        if (v.contains("detail")) b.extra["detail"] = v["detail"];
        out.push_back(std::move(b));
        return true;
    }
    if (type == "input_image") {
        const json &url = v.contains("image_url") ? v["image_url"] : v;
        ir::ContentBlock b;
        image_from_url(url.is_string() ? url.get<std::string>()
                                       : url.value("url", ""), b);
        if (v.contains("detail")) b.extra["detail"] = v["detail"];
        out.push_back(std::move(b));
        return true;
    }
    if (type == "image") {
        if (v.contains("source") && v["source"].is_object()) {
            const auto &source = v["source"];
            ir::ContentBlock b;
            if (source.value("type", "") == "base64") {
                b.kind = ir::ContentKind::Image;
                b.media_type = source.value("media_type", "");
                b.image_data_b64 = source.value("data", "");
            } else {
                image_from_url(source.value("url", ""), b);
            }
            out.push_back(std::move(b));
        } else if (v.contains("data") && v.contains("mimeType")) {
            ir::ContentBlock b;
            b.kind = ir::ContentKind::Image;
            b.media_type = v.value("mimeType", "");
            b.image_data_b64 = v.value("data", "");
            out.push_back(std::move(b));
        }
        return true;
    }
    if (type == "document") {
        append_file(v, out);
        return true;
    }
    if (type == "input_file" || type == "file") {
        append_file(v, out);
        return true;
    }
    if (type == "input_audio" || type == "audio") {
        append_audio(v, out);
        return true;
    }
    if (v.contains("data") && v.contains("mimeType") &&
        is_image_type(v.value("mimeType", ""))) {
        ir::ContentBlock b;
        b.kind = ir::ContentKind::Image;
        b.media_type = v.value("mimeType", "");
        b.image_data_b64 = v.value("data", "");
        out.push_back(std::move(b));
        return true;
    }
    if (v.contains("content")) {
        std::vector<ir::ContentBlock> nested;
        parse_media_content_impl(v["content"], nested, parse_json_strings,
                                 depth + 1);
        out.insert(out.end(), nested.begin(), nested.end());
        return !nested.empty();
    }
    ir::ContentBlock raw;
    raw.kind = ir::ContentKind::Text;
    raw.extra["raw"] = v;
    out.push_back(std::move(raw));
    return false;
}

void collect_requirements(const std::vector<ir::ContentBlock> &blocks,
                          MediaRequirements &r) {
    for (const auto &b : blocks) {
        r.image = r.image || b.kind == ir::ContentKind::Image;
        r.file = r.file || b.kind == ir::ContentKind::File;
        r.audio = r.audio || b.kind == ir::ContentKind::Audio;
        r.file_id = r.file_id || (!b.file_id.empty() &&
                                  b.file_data_b64.empty() && b.file_url.empty());
        r.file_url = r.file_url || (!b.file_url.empty() &&
                                   b.file_id.empty() && b.file_data_b64.empty());
        r.file_unresolved = r.file_unresolved ||
            (b.kind == ir::ContentKind::File && b.file_id.empty() &&
             b.file_url.empty() && b.file_data_b64.empty());
        r.audio_invalid = r.audio_invalid ||
            (b.kind == ir::ContentKind::Audio &&
             (b.audio_data_b64.empty() || b.audio_format.empty()));
        collect_requirements(b.nested, r);
    }
}

json file_data_value(const ir::ContentBlock &b) {
    if (b.file_data_b64.empty()) return json();
    return build_data_uri(b.media_type.empty() ? "application/octet-stream" :
                          b.media_type, b.file_data_b64);
}

void parse_media_content_impl(const json &value,
                              std::vector<ir::ContentBlock> &output,
                              bool parse_json_strings, int depth) {
    if (value.is_array()) {
        for (const auto &item : value)
            append_object(item, output, parse_json_strings, depth + 1);
    } else if (value.is_object()) {
        append_object(value, output, parse_json_strings, depth + 1);
    } else if (value.is_string()) {
        append_string(value.get<std::string>(), output, parse_json_strings,
                      depth + 1);
    } else if (!value.is_null()) {
        append_string(value.dump(), output, false, depth + 1);
    }
}

}  // namespace

void parse_media_content(const json &value,
                         std::vector<ir::ContentBlock> &output,
                         bool parse_json_strings) {
    parse_media_content_impl(value, output, parse_json_strings, 0);
}

void parse_tool_result_content(const json &value, ir::ContentBlock &result) {
    result.kind = ir::ContentKind::ToolResult;
    std::vector<ir::ContentBlock> blocks;
    parse_media_content(value, blocks, true);
    result.nested = std::move(blocks);
    result.text = tool_result_text(result);
    if (tool_result_has_media(result)) clamp_residual_text(result.nested);
    result.text = tool_result_text(result);
}

json serialize_openai_file_part(const ir::ContentBlock &b) {
    json part{{"type", "file"}};
    json file = json::object();
    if (!b.file_id.empty()) file["file_id"] = b.file_id;
    else if (!b.file_data_b64.empty()) file["file_data"] = file_data_value(b);
    if (!b.filename.empty()) file["filename"] = b.filename;
    part["file"] = std::move(file);
    return part;
}

json serialize_openai_audio_part(const ir::ContentBlock &b) {
    return json{{"type", "input_audio"},
                {"input_audio", {{"data", b.audio_data_b64},
                                  {"format", b.audio_format}}}};
}

json serialize_responses_file_part(const ir::ContentBlock &b) {
    json part{{"type", "input_file"}};
    if (!b.file_id.empty()) part["file_id"] = b.file_id;
    else if (!b.file_data_b64.empty()) part["file_data"] = file_data_value(b);
    else if (!b.file_url.empty()) part["file_url"] = b.file_url;
    if (!b.filename.empty()) part["filename"] = b.filename;
    return part;
}

json serialize_responses_audio_part(const ir::ContentBlock &b) {
    return json{{"type", "input_audio"},
                {"input_audio", {{"data", b.audio_data_b64},
                                  {"format", b.audio_format}}}};
}

json serialize_anthropic_file_part(const ir::ContentBlock &b) {
    json part{{"type", "document"}};
    if (!b.file_data_b64.empty()) {
        part["source"] = {{"type", "base64"},
                           {"media_type", b.media_type.empty()
                                                ? "application/octet-stream"
                                                : b.media_type},
                           {"data", b.file_data_b64}};
    } else {
        part["source"] = {{"type", "url"}, {"url", b.file_url}};
    }
    if (!b.filename.empty()) part["title"] = b.filename;
    return part;
}

json serialize_openai_tool_media_parts(const ir::ContentBlock &result) {
    json parts = json::array();
    for (const auto &b : result.nested) {
        if (b.kind == ir::ContentKind::Image) parts.push_back(image_block_to_openai_part(b));
        else if (b.kind == ir::ContentKind::File) parts.push_back(serialize_openai_file_part(b));
        else if (b.kind == ir::ContentKind::Audio) parts.push_back(serialize_openai_audio_part(b));
        else if (b.kind == ir::ContentKind::ToolResult) {
            json nested = serialize_openai_tool_media_parts(b);
            for (const auto &p : nested) parts.push_back(p);
        }
    }
    return parts;
}

json serialize_openai_content_blocks(const std::vector<ir::ContentBlock> &blocks) {
    json parts = json::array();
    for (const auto &b : blocks) {
        if (b.kind == ir::ContentKind::Text) parts.push_back(json{{"type", "text"}, {"text", b.text}});
        else if (b.kind == ir::ContentKind::Image) parts.push_back(image_block_to_openai_part(b));
        else if (b.kind == ir::ContentKind::File) parts.push_back(serialize_openai_file_part(b));
        else if (b.kind == ir::ContentKind::Audio) parts.push_back(serialize_openai_audio_part(b));
    }
    return parts;
}

json serialize_responses_tool_result_value(const ir::ContentBlock &result) {
    const bool media = tool_result_has_media(result) || has_raw_blocks(result.nested);
    if (!media) return result.text;
    json parts = json::array();
    for (const auto &b : result.nested) {
        if (b.kind == ir::ContentKind::Text) {
            if (b.extra.contains("raw") && b.extra["raw"].is_object())
                parts.push_back(b.extra["raw"]);
            else
                parts.push_back(json{{"type", "input_text"}, {"text", b.text}});
        }
        else if (b.kind == ir::ContentKind::Image) parts.push_back(json{{"type", "input_image"}, {"image_url", b.image_url.empty() ? build_data_uri(b.media_type, b.image_data_b64) : b.image_url}});
        else if (b.kind == ir::ContentKind::File) parts.push_back(serialize_responses_file_part(b));
        else if (b.kind == ir::ContentKind::Audio) parts.push_back(serialize_responses_audio_part(b));
    }
    return parts;
}

json serialize_anthropic_tool_result_content(const ir::ContentBlock &result) {
    json parts = json::array();
    for (const auto &b : result.nested) {
        if (b.kind == ir::ContentKind::Text) {
            if (b.extra.contains("raw") && b.extra["raw"].is_object())
                parts.push_back(b.extra["raw"]);
            else
                parts.push_back(json{{"type", "text"}, {"text", b.text}});
        }
        else if (b.kind == ir::ContentKind::Image) {
            json image{{"type", "image"}};
            if (!b.image_data_b64.empty()) image["source"] = {{"type", "base64"}, {"media_type", b.media_type}, {"data", b.image_data_b64}};
            else image["source"] = {{"type", "url"}, {"url", b.image_url}};
            parts.push_back(std::move(image));
        } else if (b.kind == ir::ContentKind::File) parts.push_back(serialize_anthropic_file_part(b));
    }
    return parts;
}

std::string tool_result_text(const ir::ContentBlock &result) {
    std::string text;
    for (const auto &b : result.nested) {
        if (b.kind == ir::ContentKind::Text) text += b.text;
        else if (b.kind == ir::ContentKind::ToolResult) text += tool_result_text(b);
    }
    return text;
}

bool tool_result_has_media(const ir::ContentBlock &result) {
    for (const auto &b : result.nested) {
        if (b.kind == ir::ContentKind::Image || b.kind == ir::ContentKind::File ||
            b.kind == ir::ContentKind::Audio || tool_result_has_media(b)) return true;
    }
    return false;
}

MediaRequirements request_media_requirements(const ir::ChatRequest &request) {
    MediaRequirements r;
    collect_requirements(request.system, r);
    // Responses requests use the ordered Item view.  Keep the legacy message
    // fallback for callers that have not migrated yet, but never inspect both
    // views: parsers mirror Items into messages and double traversal is both
    // wasteful and, more importantly, makes it easy to miss media carried by
    // a non-message Item such as function_call_output.
    const auto &items = request.items.empty() ? request.messages : request.items;
    for (const auto &m : items) collect_requirements(m.content, r);
    return r;
}

bool target_supports_media(ir::ApiFormat target,
                           const MediaRequirements &r,
                           std::string &reason) {
    if (r.file_unresolved) {
        reason = "file input has no file_id, file_data, or file_url";
        return false;
    }
    if (r.audio_invalid) {
        reason = "audio input is missing data or format";
        return false;
    }
    if (r.audio && target != ir::ApiFormat::OpenAI) {
        reason = "audio input is supported only by the OpenAI Chat bridge";
        return false;
    }
    if (r.file_id && target == ir::ApiFormat::Anthropic) {
        reason = "provider-managed file_id cannot be sent to Anthropic";
        return false;
    }
    if (r.file_url && target == ir::ApiFormat::OpenAI) {
        reason = "Chat file parts do not accept file URLs";
        return false;
    }
    return true;
}

}  // namespace fmt
