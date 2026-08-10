#include "format_responses_internal.h"

using namespace ir;

bool ResponsesCodec::parse_request(const json &in, ir::ChatRequest &out,
                                   std::string &err) const {
    if (!in.is_object()) {
        err = "Responses request must be a JSON object";
        return false;
    }
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("stream") && in["stream"].is_boolean())
        out.stream = in["stream"].get<bool>();

    if (in.contains("instructions")) {
        const json &ins = in["instructions"];
        if (ins.is_string()) {
            ContentBlock b;
            b.kind = ContentKind::Text;
            b.text = ins.get<std::string>();
            out.system.push_back(std::move(b));
        } else if (ins.is_array()) {
            for (const auto &part : ins) {
                if (!part.is_object()) continue;
                if (part.value("type", "") == "input_text" &&
                    part.contains("text") && part["text"].is_string()) {
                    ContentBlock b;
                    b.kind = ContentKind::Text;
                    b.text = part["text"].get<std::string>();
                    out.system.push_back(std::move(b));
                }
            }
        }
    }

    if (in.contains("input")) {
        const json &input = in["input"];
        if (input.is_string()) {
            Message msg;
            msg.role = "user";
            ContentBlock b;
            b.kind = ContentKind::Text;
            b.text = input.get<std::string>();
            msg.content.push_back(std::move(b));
            out.messages.push_back(std::move(msg));
        } else if (input.is_array()) {
            for (const auto &item : input) {
                if (!item.is_object()) continue;
                std::string type = item.value("type", "");
                if (type == "message") {
                    Message msg;
                    msg.role = item.value("role", "user");
                    if (item.contains("content"))
                        parse_responses_content(item["content"], msg.content);
                    out.messages.push_back(std::move(msg));
                } else if (type == "function_call") {
                    Message msg;
                    msg.role = "assistant";
                    ContentBlock b;
                    b.kind = ContentKind::ToolUse;
                    b.tool_call_id = item.value("call_id", "");
                    b.tool_name = item.value("name", "");
                    if (item.contains("arguments") && item["arguments"].is_string())
                        b.tool_input = parse_responses_arguments(item["arguments"].get<std::string>());
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                } else if (type == "function_call_output") {
                    Message msg;
                    msg.role = "tool";
                    ContentBlock b;
                    b.kind = ContentKind::ToolResult;
                    b.tool_use_id = item.value("call_id", "");
                    const json &o = item.contains("output") ? item["output"] : json(nullptr);
                    if (o.is_string()) b.text = o.get<std::string>();
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                } else {
                    // Unknown input item (reasoning, computer_call, ...) → keep raw.
                    out.extras["raw_input_items"].push_back(item);
                }
            }
        }
    }

    if (in.contains("tools") && in["tools"].is_array()) {
        for (const auto &t : in["tools"]) {
            if (!t.is_object()) continue;
            Tool tool;
            tool.name = t.value("name", "");
            tool.description = t.value("description", "");
            if (t.contains("parameters") && t["parameters"].is_object())
                tool.input_schema = t["parameters"];
            if (t.contains("type")) tool.extra["type"] = t["type"];
            if (t.contains("strict")) tool.extra["strict"] = t["strict"];
            // Keep the original definition so OpenAI serialization can embed
            // it (custom-tool descriptions carry the full freeform contract).
            tool.extra["raw"] = t;
            out.tools.push_back(std::move(tool));
        }
    }
    if (in.contains("tool_choice")) {
        if (in["tool_choice"].is_string())
            out.tool_choice = in["tool_choice"];
        else if (in["tool_choice"].is_object())
            out.tool_choice = in["tool_choice"];
    }

    if (in.contains("reasoning") && in["reasoning"].is_object()) {
        if (in["reasoning"].contains("effort") && in["reasoning"]["effort"].is_string()) {
            out.reasoning.enabled = true;
            out.reasoning.effort = in["reasoning"]["effort"].get<std::string>();
        }
        out.reasoning.extra = in["reasoning"];
    }
    if (in.contains("max_output_tokens") && in["max_output_tokens"].is_number_integer())
        out.max_tokens = in["max_output_tokens"].get<int>();
    if (in.contains("temperature") && in["temperature"].is_number())
        out.temperature = in["temperature"].get<double>();

    for (const auto &it : in.items()) {
        if (!responses_request_key_consumed(it.key()))
            out.extras[it.key()] = it.value();
    }
    if (in.contains("previous_response_id"))
        out.extras["previous_response_id"] = in["previous_response_id"];
    return true;
}

json ResponsesCodec::serialize_request(const ir::ChatRequest &in) const {
    json body = fmt::filter_keys(in.extras, {"store", "include", "metadata",
                                             "user", "text",
                                             "parallel_tool_calls"});
    body["model"] = in.model;
    body["stream"] = in.stream;
    if (in.max_tokens.has_value())
        body["max_output_tokens"] = *in.max_tokens;
    if (in.temperature.has_value())
        body["temperature"] = *in.temperature;
    if (in.reasoning.enabled && !in.reasoning.effort.empty()) {
        body["reasoning"] = json::object();
        body["reasoning"]["effort"] = in.reasoning.effort;
    } else if (in.reasoning.extra.is_object() && !in.reasoning.extra.empty()) {
        body["reasoning"] = in.reasoning.extra;
    }
    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = in.tool_choice;

    if (!in.tools.empty()) {
        json arr = json::array();
        for (const auto &t : in.tools) {
            json j;
            j["type"] = t.extra.contains("type") ? t.extra["type"].get<std::string>()
                                                 : std::string("function");
            j["name"] = t.name;
            j["description"] = t.description;
            j["parameters"] = t.input_schema.is_object() ? t.input_schema : json::object();
            if (t.extra.contains("strict")) j["strict"] = t.extra["strict"];
            arr.push_back(std::move(j));
        }
        body["tools"] = std::move(arr);
    }

    // Instructions from system.
    if (!in.system.empty()) {
        std::string sys_text;
        bool all_text = true;
        for (const auto &b : in.system) {
            if (b.kind == ContentKind::Text) sys_text += b.text;
            else all_text = false;
        }
        if (all_text)
            body["instructions"] = sys_text;
        else
            body["instructions"] = serialize_responses_content(in.system, false);
    }

    // Input items.
    json input = json::array();
    if (in.extras.contains("raw_input_items") && in.extras["raw_input_items"].is_array()) {
        for (const auto &it : in.extras["raw_input_items"])
            input.push_back(it);
    }
    for (const auto &m : in.messages) {
        bool has_tool_result = false;
        bool has_tool_use = false;
        for (const auto &b : m.content) {
            if (b.kind == ContentKind::ToolResult) has_tool_result = true;
            if (b.kind == ContentKind::ToolUse) has_tool_use = true;
        }
        if (has_tool_result) {
            for (const auto &b : m.content) {
                if (b.kind != ContentKind::ToolResult) continue;
                json item;
                item["type"] = "function_call_output";
                item["call_id"] = b.tool_use_id;
                item["output"] = b.text;
                input.push_back(std::move(item));
            }
        } else if (has_tool_use) {
            for (const auto &b : m.content) {
                if (b.kind != ContentKind::ToolUse) continue;
                json item;
                item["type"] = "function_call";
                item["call_id"] = b.tool_call_id;
                item["name"] = b.tool_name;
                item["arguments"] = b.tool_input.dump();
                input.push_back(std::move(item));
            }
        } else {
            json item;
            item["type"] = "message";
            item["role"] = m.role;
            item["content"] = serialize_responses_content(m.content, false);
            input.push_back(std::move(item));
        }
    }
    body["input"] = std::move(input);
    return body;
}
