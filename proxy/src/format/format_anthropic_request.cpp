#include "format_anthropic_internal.h"

using namespace ir;

bool AnthropicCodec::parse_request(const json &in, ir::ChatRequest &out,
                                   std::string &err) const {
    if (!in.is_object()) {
        err = "Anthropic request must be a JSON object";
        return false;
    }
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("stream") && in["stream"].is_boolean())
        out.stream = in["stream"].get<bool>();

    if (in.contains("system")) {
        const json &s = in["system"];
        if (s.is_string()) {
            ContentBlock b;
            b.kind = ContentKind::Text;
            b.text = s.get<std::string>();
            out.system.push_back(std::move(b));
        } else if (s.is_array()) {
            parse_anthropic_blocks(s, out.system);
        }
    }

    if (in.contains("messages") && in["messages"].is_array()) {
        for (const auto &m : in["messages"]) {
            if (!m.is_object()) continue;
            Message msg;
            msg.role = m.value("role", "");
            const json &c = m.contains("content") ? m["content"] : json(nullptr);
            if (c.is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Text;
                b.text = c.get<std::string>();
                msg.content.push_back(std::move(b));
            } else if (c.is_array()) {
                parse_anthropic_blocks(c, msg.content);
            }
            out.messages.push_back(std::move(msg));
        }
    }

    if (in.contains("tools") && in["tools"].is_array()) {
        for (const auto &t : in["tools"]) {
            if (!t.is_object()) continue;
            Tool tool;
            tool.name = t.value("name", "");
            tool.description = t.value("description", "");
            if (t.contains("input_schema") && t["input_schema"].is_object())
                tool.input_schema = t["input_schema"];
            if (t.contains("cache_control"))
                tool.extra["cache_control"] = t["cache_control"];
            out.tools.push_back(std::move(tool));
        }
    }
    if (in.contains("tool_choice"))
        out.tool_choice = in["tool_choice"];

    if (in.contains("thinking") && in["thinking"].is_object()) {
        const json &th = in["thinking"];
        std::string tt = th.value("type", "");
        if (tt == "enabled") {
            out.reasoning.enabled = true;
            if (th.contains("budget_tokens") && th["budget_tokens"].is_number_integer())
                out.reasoning.budget_tokens = th["budget_tokens"].get<int>();
        }
        out.reasoning.extra = th;
    }

    if (in.contains("max_tokens") && in["max_tokens"].is_number_integer())
        out.max_tokens = in["max_tokens"].get<int>();
    if (in.contains("temperature") && in["temperature"].is_number())
        out.temperature = in["temperature"].get<double>();

    if (in.contains("stop_sequences") && in["stop_sequences"].is_array()) {
        for (const auto &s : in["stop_sequences"])
            if (s.is_string()) out.stop_sequences.push_back(s.get<std::string>());
    }

    for (const auto &it : in.items()) {
        if (!anthropic_request_key_consumed(it.key()))
            out.extras[it.key()] = it.value();
    }
    return true;
}

json serialize_anthropic_blocks(const std::vector<ContentBlock> &blocks) {
    json arr = json::array();
    for (const auto &b : blocks) {
        switch (b.kind) {
            case ContentKind::Text: {
                json j;
                j["type"] = "text";
                j["text"] = b.text;
                if (b.extra.contains("cache_control"))
                    j["cache_control"] = b.extra["cache_control"];
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::Image: {
                json j;
                j["type"] = "image";
                if (!b.image_data_b64.empty()) {
                    j["source"]["type"] = "base64";
                    j["source"]["media_type"] = b.media_type;
                    j["source"]["data"] = b.image_data_b64;
                } else {
                    j["source"]["type"] = "url";
                    j["source"]["url"] = b.image_url;
                }
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::File:
                arr.push_back(fmt::serialize_anthropic_file_part(b));
                break;
            case ContentKind::ToolUse: {
                json j;
                j["type"] = "tool_use";
                j["id"] = b.tool_call_id;
                j["name"] = b.tool_name;
                j["input"] = b.tool_input.is_object() ? b.tool_input : json::object();
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::ToolResult: {
                json j;
                j["type"] = "tool_result";
                j["tool_use_id"] = b.tool_use_id;
                j["content"] = fmt::serialize_anthropic_tool_result_content(b);
                if (b.extra.contains("is_error")) j["is_error"] = b.extra["is_error"];
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::Thinking: {
                json j;
                if (b.extra.contains("redacted") && b.extra["redacted"].get<bool>()) {
                    j["type"] = "redacted_thinking";
                    if (b.extra.contains("data")) j["data"] = b.extra["data"];
                } else {
                    j["type"] = "thinking";
                    j["thinking"] = b.text;
                    if (b.extra.contains("signature"))
                        j["signature"] = b.extra["signature"];
                }
                arr.push_back(std::move(j));
                break;
            }
        }
    }
    return arr;
}

json AnthropicCodec::serialize_request(const ir::ChatRequest &in) const {
    json body = fmt::filter_keys(in.extras, {"top_p", "top_k", "metadata",
                                             "service_tier"});

    body["model"] = in.model;
    int max_tokens = in.max_tokens.value_or(0);
    if (max_tokens <= 0) {
        max_tokens = 4096;
        TB_LOG_ERROR( "[Anthropic] max_tokens missing, defaulting to %d\n",
                max_tokens);
    }
    body["max_tokens"] = max_tokens;
    body["stream"] = in.stream;

    if (in.reasoning.enabled) {
        json th;
        th["type"] = "enabled";
        if (in.reasoning.budget_tokens.has_value())
            th["budget_tokens"] = *in.reasoning.budget_tokens;
        else if (!in.reasoning.effort.empty())
            th["budget_tokens"] = anthropic_effort_budget(in.reasoning.effort);
        body["thinking"] = std::move(th);
    } else if (in.reasoning.extra.contains("type")) {
        body["thinking"] = in.reasoning.extra;
    }

    if (in.temperature.has_value()) body["temperature"] = *in.temperature;
    if (!in.stop_sequences.empty()) body["stop_sequences"] = in.stop_sequences;
    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = fmt::normalize_tool_choice_to_anthropic(in.tool_choice);

    if (!in.tools.empty()) {
        json arr = json::array();
        for (const auto &t : in.tools) {
            json j;
            j["name"] = t.name;
            j["description"] = t.description;
            j["input_schema"] = t.input_schema.is_object() ? t.input_schema
                                                           : json::object();
            if (t.extra.contains("cache_control"))
                j["cache_control"] = t.extra["cache_control"];
            arr.push_back(std::move(j));
        }
        body["tools"] = std::move(arr);
    }

    // System: top-level string or block array.
    if (!in.system.empty()) {
        bool all_text = true;
        std::string joined;
        for (const auto &b : in.system) {
            if (b.kind != ContentKind::Text) { all_text = false; break; }
            joined += b.text;
        }
        if (all_text)
            body["system"] = joined;
        else
            body["system"] = serialize_anthropic_blocks(in.system);
    }

    json msgs = json::array();
    for (const auto &m : in.messages) {
        json jm;
        bool has_tool_result = false;
        for (const auto &b : m.content)
            has_tool_result = has_tool_result || b.kind == ContentKind::ToolResult;
        jm["role"] = has_tool_result ? "user" : m.role;
        jm["content"] = serialize_anthropic_blocks(m.content);
        msgs.push_back(std::move(jm));
    }
    body["messages"] = std::move(msgs);
    return body;
}
