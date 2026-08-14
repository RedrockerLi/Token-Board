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
        } else if (ins.is_array()) parse_responses_content(ins, out.system);
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
                const std::string item_phase = item.value("phase", "");
                const std::string item_namespace = item.value("namespace", "");
                const json item_execution = item.value("execution", json::object());
                if (type == "message" ||
                    (type.empty() && item.contains("role") &&
                     item.contains("content"))) {
                    Message msg;
                    msg.role = item.value("role", "user");
                    msg.item_kind = ItemKind::Message;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.extra["raw_item"] = item;
                    if (item.contains("content"))
                        parse_responses_content(item["content"], msg.content);
                    out.messages.push_back(std::move(msg));
                } else if (type == "function_call") {
                    Message msg;
                    msg.role = "assistant";
                    msg.item_kind = ItemKind::FunctionCall;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.phase = item_phase;
                    msg.call_id = item.value("call_id", "");
                    msg.name = item.value("name", "");
                    msg.namespace_name = item_namespace;
                    msg.payload.clear();
                    msg.execution = item_execution;
                    msg.extra["raw_item"] = item;
                    ContentBlock b;
                    b.kind = ContentKind::ToolUse;
                    b.tool_call_id = item.value("call_id", "");
                    b.tool_name = item.value("name", "");
                    if (item.contains("arguments")) {
                        if (!item["arguments"].is_string()) {
                            err = "function_call arguments must be a JSON string";
                            return false;
                        }
                        msg.payload = item["arguments"].get<std::string>();
                        b.extra["raw_arguments"] = msg.payload;
                        try { b.tool_input = json::parse(msg.payload); }
                        catch (...) { err = "invalid JSON in function_call arguments"; return false; }
                    }
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                } else if (type == "function_call_output" ||
                           type == "tool_search_output") {
                    Message msg;
                    msg.role = "tool";
                    msg.item_kind = type == "tool_search_output"
                        ? ItemKind::ToolSearchOutput : ItemKind::FunctionCallOutput;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.call_id = item.value("call_id", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.payload = item.value("output", json()).dump();
                    msg.extra["raw_item"] = item;
                    ContentBlock b;
                    b.kind = ContentKind::ToolResult;
                    b.tool_use_id = item.value("call_id", "");
                    const json &o = item.contains("output") ? item["output"] : json(nullptr);
                    fmt::parse_tool_result_content(o, b);
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                    if (type == "tool_search_output" && o.is_object() &&
                        o.contains("tools") && o["tools"].is_array()) {
                        for (const auto &dynamic : o["tools"]) {
                            if (!dynamic.is_object()) continue;
                            Tool tool;
                            tool.wire_type = dynamic.value("type", "function");
                            tool.kind = tool.wire_type == "function" ? ToolKind::Function : ToolKind::Hosted;
                            tool.name = dynamic.value("name", "");
                            tool.description = dynamic.value("description", "");
                            tool.input_schema = dynamic.contains("parameters")
                                ? dynamic["parameters"] : dynamic.value("input_schema", json::object());
                            tool.raw = dynamic;
                            tool.extra["type"] = tool.wire_type;
                            out.tools.push_back(std::move(tool));
                        }
                    }
                } else if (type == "custom_tool_call") {
                    Message msg;
                    msg.role = "assistant";
                    msg.item_kind = ItemKind::CustomToolCall;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.call_id = item.value("call_id", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.name = item.value("name", "custom_tool");
                    if (item.contains("input") && !item["input"].is_string()) {
                        err = "custom_tool_call input must be a string";
                        return false;
                    }
                    msg.payload = item.value("input", "");
                    msg.extra["raw_item"] = item;
                    ContentBlock b;
                    b.kind = ContentKind::ToolUse;
                    b.tool_call_id = item.value("call_id", "");
                    b.tool_name = item.value("name", "custom_tool");
                    b.extra["type"] = "custom";
                    b.tool_input = json{{"input", item.value("input", "")}};
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                } else if (type == "custom_tool_call_output") {
                    Message msg;
                    msg.role = "tool";
                    msg.item_kind = ItemKind::CustomToolCallOutput;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.call_id = item.value("call_id", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.extra["raw_item"] = item;
                    ContentBlock b;
                    b.kind = ContentKind::ToolResult;
                    b.extra["type"] = "custom";
                    b.tool_use_id = item.value("call_id", "");
                    const json &o = item.contains("output") ? item["output"] : json(nullptr);
                    fmt::parse_tool_result_content(o, b);
                    msg.content.push_back(std::move(b));
                    out.messages.push_back(std::move(msg));
                } else if (type == "tool_search_call") {
                    Message msg;
                    msg.role = "assistant";
                    msg.item_kind = ItemKind::ToolSearchCall;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.call_id = item.value("call_id", "");
                    if (item.contains("arguments") && !item["arguments"].is_object()) {
                        err = "tool_search_call arguments must be an object";
                        return false;
                    }
                    msg.payload = item.value("arguments", json::object()).dump();
                    msg.execution = item.value("execution", json::object());
                    msg.extra["raw_item"] = item;
                    out.messages.push_back(std::move(msg));
                } else if (type == "reasoning") {
                    Message msg;
                    msg.role = "assistant";
                    msg.item_kind = ItemKind::Reasoning;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.encrypted_content = item.value("encrypted_content", "");
                    msg.extra["raw_item"] = item;
                    if (item.contains("summary") && item["summary"].is_array()) {
                        for (const auto &part : item["summary"])
                            if (part.is_object() && part.contains("text") &&
                                part["text"].is_string()) {
                                ContentBlock b;
                                b.kind = ContentKind::Thinking;
                                b.text = part.value("text", "");
                                msg.content.push_back(std::move(b));
                            }
                    }
                    out.messages.push_back(std::move(msg));
                } else {
                    Message msg;
                    msg.role = item.value("role", "");
                    msg.item_kind = ItemKind::Opaque;
                    msg.id = item.value("id", "");
                    msg.status = item.value("status", "");
                    msg.phase = item_phase;
                    msg.namespace_name = item_namespace;
                    msg.execution = item_execution;
                    msg.extra["raw_item"] = item;
                    out.messages.push_back(std::move(msg));
                }
            }
        }
    }
    std::uint64_t group = 0; bool assistant_group = false;
    for (auto &item : out.messages) {
        const bool assistant = item.role == "assistant";
        if (!assistant) { assistant_group = false; continue; }
        if (!assistant_group) { ++group; assistant_group = true; }
        item.group_id = group;
    }
    if (in.contains("tools") && in["tools"].is_array()) {
        for (const auto &t : in["tools"]) {
            if (!t.is_object()) continue;
            Tool tool;
            tool.wire_type = t.value("type", "function");
            tool.name = t.value("name", "");
            tool.description = t.value("description", "");
            tool.raw = t;
            if (tool.wire_type == "custom") tool.kind = ToolKind::Custom;
            else if (tool.wire_type == "tool_search") tool.kind = ToolKind::ToolSearch;
            else if (tool.wire_type == "namespace") {
                tool.kind = ToolKind::Namespace;
                const auto &children = t.contains("tools") ? t["tools"] : t.value("children", json::array());
                if (children.is_array()) {
                    for (const auto &child : children) {
                        if (!child.is_object()) continue;
                        Tool c;
                        c.wire_type = child.value("type", "function");
                        c.kind = c.wire_type == "custom" ? ToolKind::Custom
                            : c.wire_type == "function" ? ToolKind::Function
                            : ToolKind::Hosted;
                        c.name = child.value("name", "");
                        c.description = child.value("description", "");
                        c.raw = child;
                        c.extra["raw"] = child;
                        if (child.contains("parameters")) c.input_schema = child["parameters"];
                        tool.children.push_back(std::move(c));
                    }
                }
            } else if (tool.wire_type != "function") tool.kind = ToolKind::Hosted;
            if (t.contains("parameters") && t["parameters"].is_object())
                tool.input_schema = t["parameters"];
            if (t.contains("type")) tool.extra["type"] = t["type"];
            if (t.contains("strict")) tool.extra["strict"] = t["strict"];
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
        out.reasoning.enabled = true;
        if (in["reasoning"].contains("effort") && in["reasoning"]["effort"].is_string()) {
            out.reasoning.effort = in["reasoning"]["effort"].get<std::string>();
        }
        out.reasoning.extra = in["reasoning"];
    }
    if (in.contains("max_output_tokens") && in["max_output_tokens"].is_number_integer())
        out.max_tokens = in["max_output_tokens"].get<int>();
    if (in.contains("temperature") && in["temperature"].is_number())
        out.temperature = in["temperature"].get<double>();
    if (in.contains("text") && in["text"].is_object() &&
        in["text"].contains("format") && in["text"]["format"].is_object()) {
        const auto &f = in["text"]["format"];
        out.structured_output.raw = f;
        const std::string type = f.value("type", "text");
        if (type == "json_schema") {
            out.structured_output.kind = StructuredOutputKind::JsonSchema;
            out.structured_output.name = f.value("name", "");
            out.structured_output.description = f.value("description", "");
            if (f.contains("schema")) out.structured_output.schema = f["schema"];
            out.structured_output.strict = f.value("strict", false);
        } else if (type == "json_object") {
            out.structured_output.kind = StructuredOutputKind::JsonObject;
        } else {
            out.structured_output.kind = StructuredOutputKind::Text;
        }
    }
    for (const auto &it : in.items()) {
        if (!responses_request_key_consumed(it.key()))
            out.extras[it.key()] = it.value();
    }
    static constexpr const char *passthrough_keys[] = {
        "background", "context_management", "conversation", "include",
        "max_tool_calls", "metadata", "moderation", "parallel_tool_calls",
        "previous_response_id", "prompt", "prompt_cache_key",
        "prompt_cache_options", "prompt_cache_retention", "safety_identifier",
        "service_tier", "store", "stream_options", "text", "top_logprobs",
        "top_p", "truncation", "user",
    };
    for (const char *key : passthrough_keys)
        if (in.contains(key)) out.extras[key] = in[key];
    out.items = out.messages;
    return true;
}
json ResponsesCodec::serialize_request(const ir::ChatRequest &in,
                                       const ir::ConversionContext *context) const {
    json body = fmt::filter_keys(
        in.extras,
        {"background", "context_management", "conversation", "include",
         "max_tool_calls", "metadata", "moderation", "parallel_tool_calls",
         "previous_response_id", "prompt", "prompt_cache_key",
         "prompt_cache_options", "prompt_cache_retention", "safety_identifier",
         "service_tier", "store", "stream_options", "text", "top_logprobs",
         "top_p", "truncation", "user"});
    body["model"] = in.model;
    body["stream"] = in.stream;
    if (in.max_tokens.has_value())
        body["max_output_tokens"] = *in.max_tokens;
    if (in.temperature.has_value())
        body["temperature"] = *in.temperature;
    const json original_text = body.value("text", json::object());
    auto set_text_format = [&](json format) {
        json text = json::object();
        if (original_text.is_object()) {
            for (const auto &entry : original_text.items())
                if (entry.key() != "format") text[entry.key()] = entry.value();
        }
        text["format"] = std::move(format);
        body["text"] = std::move(text);
    };
    if (in.structured_output.kind == StructuredOutputKind::JsonObject) {
        set_text_format(json{{"type", "json_object"}});
    } else if (in.structured_output.kind == StructuredOutputKind::JsonSchema) {
        set_text_format(json{
            {"type", "json_schema"},
            {"name", in.structured_output.name},
            {"description", in.structured_output.description},
            {"schema", in.structured_output.schema},
            {"strict", in.structured_output.strict}
        });
    } else if (in.structured_output.raw.is_object() &&
               !in.structured_output.raw.empty()) {
        set_text_format(in.structured_output.raw);
    }
    if (in.reasoning.enabled) {
        json include = body.value("include", json::array());
        if (!include.is_array()) include = json::array();
        bool present = false;
        for (const auto &entry : include)
            if (entry.is_string() && entry.get<std::string>() == "reasoning.encrypted_content") present = true;
        if (!present) include.push_back("reasoning.encrypted_content");
        body["include"] = std::move(include);
    }
    if (in.reasoning.enabled ||
        (in.reasoning.extra.is_object() && !in.reasoning.extra.empty())) {
        json reasoning = in.reasoning.extra.is_object()
            ? in.reasoning.extra : json::object();
        if (!in.reasoning.effort.empty()) reasoning["effort"] = in.reasoning.effort;
        body["reasoning"] = std::move(reasoning);
    }
    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = fmt::normalize_tool_choice_for_target(
            in.tool_choice, context, ApiFormat::OpenAIResponses);
    if (!in.tools.empty()) {
        std::vector<Tool> serialized_tools;
        const std::vector<Tool> *tool_view = &in.tools;
        if (context && context->source == ApiFormat::OpenAIResponses &&
            context->target == ApiFormat::OpenAIResponses &&
            !context->tools.source_tools.empty()) {
            for (const auto &source : context->tools.source_tools) {
                if (source.kind == ToolKind::Namespace) {
                    bool mapped_child = false;
                    for (const auto &mapped : context->tools.target_tools) {
                        for (const auto &mapping : context->tools.mappings) {
                            if (mapping.flat_name == mapped.name &&
                                mapping.namespace_name == source.name) {
                                serialized_tools.push_back(mapped);
                                mapped_child = true;
                                break;
                            }
                        }
                    }
                    if (!mapped_child) {
                        Tool raw_namespace = source;
                        raw_namespace.kind = ToolKind::Hosted;
                        serialized_tools.push_back(std::move(raw_namespace));
                    }
                } else if (source.kind == ToolKind::ToolSearch) {
                    for (const auto &mapped : context->tools.target_tools)
                        if (mapped.kind == ToolKind::ToolSearch)
                            serialized_tools.push_back(mapped);
                } else {
                    serialized_tools.push_back(source);
                }
            }
            tool_view = &serialized_tools;
        }
        json arr = json::array();
        for (const auto &t : *tool_view) {
            if (t.kind == ToolKind::Hosted && t.raw.is_object()) {
                arr.push_back(t.raw);
                continue;
            }
            const std::string type =
                t.extra.contains("type") && t.extra["type"].is_string()
                    ? t.extra["type"].get<std::string>() : "function";
            if (type == "custom" && t.extra.contains("raw") &&
                t.extra["raw"].is_object()) {
                json raw = t.extra["raw"];
                if (!t.name.empty()) raw["name"] = t.name;
                if (!t.description.empty()) raw["description"] = t.description;
                arr.push_back(std::move(raw));
                continue;
            }
            json j;
            j["type"] = type;
            j["name"] = t.name;
            j["description"] = t.description;
            j["parameters"] = t.input_schema.is_object() ? t.input_schema : json::object();
            if (t.extra.contains("strict")) j["strict"] = t.extra["strict"];
            arr.push_back(std::move(j));
        }
        body["tools"] = std::move(arr);
    }
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
    body["input"] = serialize_responses_input(in, context);
    return body;
}
