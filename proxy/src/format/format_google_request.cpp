#include "format_google_internal.h"

#include <algorithm>

using namespace ir;

namespace {

/// Gemini has exactly two content roles: `user` and `model`.
bool role_is_model(const std::string &role) {
    return role == "assistant" || role == "model";
}

/// Append a content, merging into the previous turn when the role repeats
/// (Gemini expects one Content per turn, not one per IR item).
void append_content(json &contents, const std::string &role, json parts) {
    if (parts.empty()) return;
    if (!contents.empty() && contents.back().is_object() &&
        contents.back().value("role", "") == role) {
        for (auto &part : parts)
            contents.back()["parts"].push_back(std::move(part));
        return;
    }
    json content;
    content["role"] = role;
    content["parts"] = std::move(parts);
    contents.push_back(std::move(content));
}

bool consumed_request_key(const std::string &key) {
    static constexpr const char *consumed[] = {
        "model", "stream", "contents", "systemInstruction", "tools",
        "toolConfig", "generationConfig"};
    for (const char *item : consumed)
        if (key == item) return true;
    return false;
}

void parse_generation_config(const json &gc, ir::ChatRequest &out) {
    if (!gc.is_object()) return;
    if (gc.contains("maxOutputTokens") && gc["maxOutputTokens"].is_number_integer())
        out.max_tokens = gc["maxOutputTokens"].get<int>();
    if (gc.contains("temperature") && gc["temperature"].is_number())
        out.temperature = gc["temperature"].get<double>();
    if (gc.contains("stopSequences") && gc["stopSequences"].is_array()) {
        for (const auto &stop : gc["stopSequences"])
            if (stop.is_string()) out.stop_sequences.push_back(stop.get<std::string>());
    }
    if (gc.contains("topP") && gc["topP"].is_number())
        out.extras["top_p"] = gc["topP"];
    if (gc.contains("topK") && gc["topK"].is_number_integer())
        out.extras["top_k"] = gc["topK"];

    if (gc.contains("thinkingConfig") && gc["thinkingConfig"].is_object()) {
        const json &thinking = gc["thinkingConfig"];
        if (thinking.contains("thinkingBudget") &&
            thinking["thinkingBudget"].is_number_integer()) {
            const int budget = thinking["thinkingBudget"].get<int>();
            // 0 disables thinking, a negative budget asks the model to decide.
            out.reasoning.enabled = budget != 0;
            if (budget > 0) out.reasoning.budget_tokens = budget;
        } else if (thinking.contains("thinkingLevel") &&
                   thinking["thinkingLevel"].is_string()) {
            out.reasoning.enabled = true;
            std::string level = thinking["thinkingLevel"].get<std::string>();
            std::transform(level.begin(), level.end(), level.begin(),
                           [](unsigned char ch) {
                               return static_cast<char>(std::tolower(ch));
                           });
            out.reasoning.effort = std::move(level);
        }
        out.extras["google_thinking_config"] = thinking;
    }

    if (gc.contains("responseJsonSchema") && gc["responseJsonSchema"].is_object()) {
        out.structured_output.kind = StructuredOutputKind::JsonSchema;
        out.structured_output.schema = gc["responseJsonSchema"];
        out.structured_output.raw = gc;
    } else if (gc.contains("responseSchema") && gc["responseSchema"].is_object()) {
        out.structured_output.kind = StructuredOutputKind::JsonSchema;
        out.structured_output.schema = gfmt::schema_from_wire(gc["responseSchema"]);
        out.structured_output.raw = gc;
    } else if (gc.value("responseMimeType", "") == "application/json") {
        out.structured_output.kind = StructuredOutputKind::JsonObject;
        out.structured_output.raw = gc;
    }

    json leftover = gc;
    for (const char *key : {"maxOutputTokens", "temperature", "stopSequences",
                            "topP", "topK", "thinkingConfig",
                            "responseMimeType", "responseSchema",
                            "responseJsonSchema"})
        leftover.erase(key);
    if (!leftover.empty()) out.extras["google_generation_config"] = leftover;
}

void parse_tool_config(const json &config, ir::ChatRequest &out) {
    if (!config.is_object()) return;
    out.extras["google_tool_config"] = config;
    if (!config.contains("functionCallingConfig") ||
        !config["functionCallingConfig"].is_object())
        return;
    const json &call = config["functionCallingConfig"];
    const std::string mode = call.value("mode", "");
    if (mode == "NONE") {
        out.tool_choice = "none";
    } else if (mode == "ANY") {
        const bool names = call.contains("allowedFunctionNames") &&
            call["allowedFunctionNames"].is_array() &&
            call["allowedFunctionNames"].size() == 1 &&
            call["allowedFunctionNames"][0].is_string();
        if (names) {
            out.tool_choice = json{
                {"type", "function"},
                {"function",
                 json{{"name", call["allowedFunctionNames"][0]}}}};
        } else {
            out.tool_choice = "required";
        }
    } else if (mode == "AUTO" || mode == "VALIDATED") {
        // VALIDATED constrains decoding without changing who may call the
        // tool; "auto" is the closest portable request shape.
        out.tool_choice = "auto";
    }
}

void parse_tools(const json &tools, ir::ChatRequest &out) {
    if (!tools.is_array()) return;
    for (const auto &entry : tools) {
        if (!entry.is_object()) continue;
        if (entry.contains("functionDeclarations") &&
            entry["functionDeclarations"].is_array()) {
            for (const auto &decl : entry["functionDeclarations"]) {
                if (!decl.is_object()) continue;
                Tool tool;
                tool.kind = ToolKind::Function;
                tool.name = decl.value("name", "");
                tool.description = decl.value("description", "");
                if (decl.contains("parametersJsonSchema") &&
                    decl["parametersJsonSchema"].is_object())
                    tool.input_schema = decl["parametersJsonSchema"];
                else if (decl.contains("parameters") &&
                         decl["parameters"].is_object())
                    tool.input_schema = gfmt::schema_from_wire(decl["parameters"]);
                // Keep the declaration verbatim so a same-format replay keeps
                // Gemini-only knobs (`response`, `behavior`, …).
                tool.extra["google_declaration"] = decl;
                out.tools.push_back(std::move(tool));
            }
            continue;
        }
        // Provider-executed tools (googleSearch, codeExecution, urlContext, …)
        // have no IR representation; keep them raw for a same-format replay.
        Tool hosted;
        hosted.kind = ToolKind::Hosted;
        hosted.raw = entry;
        for (const auto &field : entry.items()) {
            hosted.name = field.key();
            hosted.wire_type = field.key();
            break;
        }
        out.tools.push_back(std::move(hosted));
    }
}

}  // namespace

bool GoogleCodec::parse_request(const json &in, ir::ChatRequest &out,
                                std::string &err) const {
    if (!in.is_object()) {
        err = "Gemini request must be a JSON object";
        return false;
    }
    // `model` and `stream` live in the URL on this wire; the HTTP layer fills
    // them in.  Accept a body field anyway for tolerant clients.
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();

    if (in.contains("systemInstruction")) {
        const json &system = in["systemInstruction"];
        if (system.is_string()) {
            ContentBlock block;
            block.kind = ContentKind::Text;
            block.text = system.get<std::string>();
            out.system.push_back(std::move(block));
        } else if (system.is_object() && system.contains("parts")) {
            gfmt::blocks_from_parts(system["parts"], out.system);
        }
    }

    if (in.contains("contents") && in["contents"].is_array()) {
        for (const auto &content : in["contents"]) {
            if (!content.is_object()) continue;
            Message message;
            message.role = role_is_model(content.value("role", "user"))
                ? "assistant" : "user";
            if (content.contains("parts"))
                gfmt::blocks_from_parts(content["parts"], message.content);
            out.messages.push_back(std::move(message));
        }
    }

    if (in.contains("tools")) parse_tools(in["tools"], out);
    if (in.contains("toolConfig")) parse_tool_config(in["toolConfig"], out);
    if (in.contains("generationConfig"))
        parse_generation_config(in["generationConfig"], out);

    for (const auto &entry : in.items()) {
        if (consumed_request_key(entry.key())) continue;
        out.extras[entry.key()] = entry.value();
    }
    out.items = out.messages;
    return true;
}

json GoogleCodec::serialize_request(const ir::ChatRequest &in,
                                    const ir::ConversionContext *context) const {
    json body = json::object();
    (void)context;

    if (!in.system.empty()) {
        json parts = json::array();
        for (const auto &block : in.system) {
            if (block.kind != ContentKind::Text) continue;
            parts.push_back(json{{"text", block.text}});
        }
        if (!parts.empty()) body["systemInstruction"] = json{{"parts", parts}};
    }

    const auto &items = in.items.empty() ? in.messages : in.items;
    const auto call_names = gfmt::tool_call_names(items);
    // Gemini 3 validates opaque thought signatures when a function call is
    // replayed; older models must not receive a synthetic one.
    const bool require_signature = gfmt::requires_thought_signature(in.model);
    json contents = json::array();
    for (const auto &item : items) {
        std::vector<ContentBlock> blocks = item.content;
        bool has_tool_result = false;
        for (auto &block : blocks) {
            if (block.kind != ContentKind::ToolResult) continue;
            has_tool_result = true;
            if (!block.tool_name.empty()) continue;
            // OpenAI tool messages identify the result by call id only.
            const auto found = call_names.find(block.tool_use_id);
            if (found != call_names.end())
                block.tool_name = found->second;
            else if (!block.tool_use_id.empty())
                block.tool_name = block.tool_use_id;
            else
                block.tool_name = "tool";
        }
        json parts = gfmt::parts_from_blocks(blocks, require_signature);
        if (parts.empty()) continue;
        const std::string role =
            (has_tool_result || !role_is_model(item.role)) ? "user" : "model";
        append_content(contents, role, std::move(parts));
    }
    if (!contents.empty()) body["contents"] = std::move(contents);

    if (!in.tools.empty()) {
        json tools = json::array();
        json declarations = json::array();
        for (const auto &tool : in.tools) {
            if (tool.kind == ToolKind::Hosted) {
                if (tool.raw.is_object() && !tool.raw.empty())
                    tools.push_back(tool.raw);
                continue;
            }
            if (tool.kind != ToolKind::Function && tool.kind != ToolKind::Custom)
                continue;
            declarations.push_back(gfmt::function_declaration(tool));
        }
        if (!declarations.empty())
            tools.push_back(json{{"functionDeclarations", declarations}});
        if (!tools.empty()) body["tools"] = std::move(tools);
    }

    if (in.extras.contains("google_tool_config") &&
        in.extras["google_tool_config"].is_object()) {
        body["toolConfig"] = in.extras["google_tool_config"];
    } else {
        json calling = gfmt::function_calling_config(in.tool_choice);
        if (!calling.empty())
            body["toolConfig"] = json{{"functionCallingConfig", calling}};
    }

    json config = gfmt::generation_config(in);
    if (!config.empty()) body["generationConfig"] = std::move(config);

    for (const char *key : {"safetySettings", "cachedContent", "labels",
                            "serviceTier"})
        if (in.extras.contains(key)) body[key] = in.extras[key];
    return body;
}
