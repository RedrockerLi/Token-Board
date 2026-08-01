#include "format_common.h"

#include <cctype>
#include <cstring>

namespace fmt {

std::string strip_one_m_suffix_for_upstream(const std::string &model) {
    static const char kMarker[] = "[1m]";
    static const size_t kLen = 4;
    std::string t = model;
    auto end = t.find_last_not_of(" \t\r\n");
    if (end == std::string::npos) return model;
    t.erase(end + 1);
    if (t.size() >= kLen) {
        bool eq = true;
        for (size_t i = 0; i < kLen; i++) {
            if (std::tolower(static_cast<unsigned char>(t[t.size() - kLen + i])) !=
                kMarker[i]) { eq = false; break; }
        }
        if (eq) {
            t.erase(t.size() - kLen);
            auto e2 = t.find_last_not_of(" \t\r\n");
            t.erase(e2 == std::string::npos ? 0 : e2 + 1);
            return t;
        }
    }
    return model;
}

bool is_reasoning_vendor(const std::string &model) {
    static const char *kHints[] = {"deepseek", "kimi", "moonshot", "mimo"};
    std::string t = model;
    for (char &c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    for (const char *h : kHints)
        if (t.find(h) != std::string::npos) return true;
    return false;
}

bool parse_data_uri(const std::string &uri, std::string &media_type,
                    std::string &b64) {
    static const char prefix[] = "data:";
    if (uri.rfind(prefix, 0) != 0) return false;
    size_t comma = uri.find(",", 5);
    if (comma == std::string::npos) return false;
    std::string meta = uri.substr(5, comma - 5);  // e.g. "image/png;base64"
    size_t semi = meta.rfind(';');
    if (semi == std::string::npos || meta.substr(semi + 1) != "base64")
        return false;
    media_type = meta.substr(0, semi);
    b64 = uri.substr(comma + 1);
    return true;
}

std::string build_data_uri(const std::string &media_type,
                           const std::string &b64) {
    return "data:" + media_type + ";base64," + b64;
}

ir::ContentBlock openai_image_part_to_block(const json &part) {
    ir::ContentBlock b;
    b.kind = ir::ContentKind::Image;
    if (part.contains("image_url") && part["image_url"].is_object()) {
        const auto &iu = part["image_url"];
        if (iu.contains("url") && iu["url"].is_string())
            b.image_url = iu["url"].get<std::string>();
        if (iu.contains("detail") && iu["detail"].is_string())
            b.extra["detail"] = iu["detail"];
    }
    std::string media, b64;
    if (parse_data_uri(b.image_url, media, b64)) {
        b.image_data_b64 = b64;
        b.media_type = media;
        b.image_url.clear();  // keep the data URI form only in extra
        b.extra["data_uri"] = "data:" + media + ";base64," + b64;
    }
    return b;
}

json image_block_to_openai_part(const ir::ContentBlock &b) {
    json part;
    part["type"] = "image_url";
    if (!b.image_url.empty()) {
        part["image_url"]["url"] = b.image_url;
    } else {
        std::string uri = build_data_uri(b.media_type, b.image_data_b64);
        part["image_url"]["url"] = uri;
    }
    if (b.extra.contains("detail"))
        part["image_url"]["detail"] = b.extra["detail"];
    return part;
}

const char *stop_reason_to_openai(ir::StopReason r) {
    switch (r) {
        case ir::StopReason::Stop: return "stop";
        case ir::StopReason::Length: return "length";
        case ir::StopReason::ToolUse: return "tool_calls";
        case ir::StopReason::ContentFilter: return "content_filter";
        default: return "stop";
    }
}

const char *stop_reason_to_anthropic(ir::StopReason r) {
    switch (r) {
        case ir::StopReason::Length: return "max_tokens";
        case ir::StopReason::ToolUse: return "tool_use";
        case ir::StopReason::ContentFilter: return "refusal";
        default: return "end_turn";
    }
}

const char *stop_reason_to_responses(ir::StopReason r) {
    return r == ir::StopReason::Length ? "incomplete" : "completed";
}

ir::StopReason openai_finish_reason_to_stop(const std::string &fr) {
    if (fr == "stop") return ir::StopReason::Stop;
    if (fr == "length") return ir::StopReason::Length;
    if (fr == "tool_calls" || fr == "function_call")
        return ir::StopReason::ToolUse;
    if (fr == "content_filter") return ir::StopReason::ContentFilter;
    return ir::StopReason::Unknown;
}

ir::StopReason anthropic_stop_reason_to_stop(const std::string &sr) {
    if (sr == "end_turn") return ir::StopReason::Stop;
    if (sr == "stop_sequence") return ir::StopReason::Stop;
    if (sr == "pause_turn") return ir::StopReason::Stop;
    if (sr == "max_tokens") return ir::StopReason::Length;
    if (sr == "tool_use") return ir::StopReason::ToolUse;
    if (sr == "refusal") return ir::StopReason::ContentFilter;
    return ir::StopReason::Unknown;
}

ir::StopReason responses_status_to_stop(const std::string &st) {
    if (st == "completed") return ir::StopReason::Stop;
    if (st == "incomplete") return ir::StopReason::Length;
    if (st == "failed") return ir::StopReason::Unknown;
    return ir::StopReason::Unknown;
}

json normalize_tool_choice_to_openai(const json &tc) {
    if (!tc.is_object()) return tc;  // strings ("auto"/"required"/"none") are already valid
    std::string type = tc.value("type", "");
    if (type == "tool") {
        // Anthropic: {"type":"tool","name":"foo"} → OpenAI function shape.
        json out;
        out["type"] = "function";
        out["function"] = {{"name", tc.value("name", "")}};
        return out;
    }
    if (type == "any") return json("required");  // Anthropic "any" → OpenAI "required"
    if (type == "auto") return json("auto");
    if (type == "none") return json("none");
    return tc;  // already OpenAI-shaped ({"type":"function",...}) or unknown → pass through
}

json normalize_tool_choice_to_anthropic(const json &tc) {
    if (tc.is_string()) {
        std::string s = tc.get<std::string>();
        if (s == "required") return json{{"type", "any"}};
        if (s == "none") return json{{"type", "none"}};
        return json{{"type", "auto"}};  // "auto" and anything else
    }
    if (tc.is_object() && tc.value("type", "") == "function") {
        // OpenAI: {"type":"function","function":{"name":"foo"}} → Anthropic tool shape.
        std::string name;
        if (tc.contains("function") && tc["function"].is_object())
            name = tc["function"].value("name", "");
        json out;
        out["type"] = "tool";
        if (!name.empty()) out["name"] = name;
        return out;
    }
    return tc;  // already Anthropic-shaped or unknown → pass through
}

json normalize_error_body(const json &body) {
    json out;
    out["message"] = "Upstream error";
    out["type"] = "upstream_error";
    out["code"] = nullptr;
    if (body.is_object()) {
        const json *e = &body;
        if (body.contains("error") && body["error"].is_object()) e = &body["error"];
        if (e->contains("message") && (*e)["message"].is_string())
            out["message"] = (*e)["message"];
        if (e->contains("type") && (*e)["type"].is_string())
            out["type"] = (*e)["type"];
        if (e->contains("code"))
            out["code"] = (*e)["code"];
        if (e->contains("param") && (*e)["param"].is_string())
            out["param"] = (*e)["param"];
    }
    return out;
}

json filter_keys(const json &src, std::initializer_list<const char *> keep) {
    json out = json::object();
    if (!src.is_object()) return out;
    for (auto it = src.begin(); it != src.end(); ++it) {
        for (const char *k : keep) {
            if (it.key() == k) {
                out[k] = it.value();
                break;
            }
        }
    }
    return out;
}

bool parse_sse_frame(const std::string &frame, std::string *event_name,
                     std::string *data) {
    if (event_name) event_name->clear();
    if (data) data->clear();
    bool has_data = false;
    size_t start = 0;
    while (start <= frame.size()) {
        size_t nl = frame.find('\n', start);
        std::string line = frame.substr(
            start, nl == std::string::npos ? std::string::npos : nl - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.rfind("event:", 0) == 0) {
            if (event_name) {
                *event_name = line.substr(6);
                if (!event_name->empty() && (*event_name)[0] == ' ')
                    event_name->erase(0, 1);
            }
        } else if (line.rfind("data:", 0) == 0) {
            has_data = true;
            std::string val = line.substr(5);
            if (!val.empty() && val[0] == ' ') val.erase(0, 1);
            if (data) *data += val;
        }
        // lines starting with ':' are SSE comments — ignored
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    return has_data;
}

ir::Usage parse_usage_json(const json &u) {
    ir::Usage out;
    if (!u.is_object()) return out;
    if (u.contains("prompt_tokens") && u["prompt_tokens"].is_number_integer())
        out.prompt_tokens = u["prompt_tokens"].get<int>();
    if (u.contains("completion_tokens") && u["completion_tokens"].is_number_integer())
        out.completion_tokens = u["completion_tokens"].get<int>();
    if (u.contains("input_tokens") && u["input_tokens"].is_number_integer())
        out.prompt_tokens = u["input_tokens"].get<int>();
    if (u.contains("output_tokens") && u["output_tokens"].is_number_integer())
        out.completion_tokens = u["output_tokens"].get<int>();
    if (u.contains("cache_read_input_tokens") && u["cache_read_input_tokens"].is_number_integer())
        out.cache_read_tokens = u["cache_read_input_tokens"].get<int>();
    if (u.contains("cache_creation_input_tokens") && u["cache_creation_input_tokens"].is_number_integer())
        out.cache_creation_tokens = u["cache_creation_input_tokens"].get<int>();
    if (u.contains("total_tokens") && u["total_tokens"].is_number_integer())
        out.total_tokens = u["total_tokens"].get<int>();
    else
        out.total_tokens = out.prompt_tokens + out.completion_tokens;
    if (u.contains("prompt_tokens_details") && u["prompt_tokens_details"].is_object() &&
        u["prompt_tokens_details"].contains("cached_tokens") &&
        u["prompt_tokens_details"]["cached_tokens"].is_number_integer())
        out.cache_read_tokens = u["prompt_tokens_details"]["cached_tokens"].get<int>();
    if (u.contains("input_tokens_details") && u["input_tokens_details"].is_object() &&
        u["input_tokens_details"].contains("cached_tokens") &&
        u["input_tokens_details"]["cached_tokens"].is_number_integer())
        out.cache_read_tokens = u["input_tokens_details"]["cached_tokens"].get<int>();
    return out;
}

}  // namespace fmt
