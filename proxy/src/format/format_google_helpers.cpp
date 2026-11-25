#include "format_google_internal.h"

#include <algorithm>
#include <cctype>
#include <limits>

namespace gfmt {
namespace {

std::string ascii_lower(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char ch) {
                       return static_cast<char>(std::tolower(ch));
                   });
    return value;
}

int clamped_int(const json &object, const char *key) {
    if (!object.is_object()) return 0;
    const auto it = object.find(key);
    if (it == object.end() || !it->is_number_integer()) return 0;
    const long long value = it->get<long long>();
    if (value < 0 || value > std::numeric_limits<int>::max()) return 0;
    return static_cast<int>(value);
}

int clamped_sum(int first, int second) {
    const long long total = static_cast<long long>(first) + second;
    return total >= 0 && total <= std::numeric_limits<int>::max()
        ? static_cast<int>(total) : 0;
}

// ── JSON Schema → Gemini Schema (OpenAPI 3.03 subset) ────────────────────

/// The Gemini `Schema` message is not a general JSON Schema: these keywords
/// have no representation there and force the `parametersJsonSchema` path.
bool schema_blocks_openapi_subset(const json &schema) {
    if (!schema.is_object()) return true;
    static constexpr const char *unsupported[] = {
        "oneOf", "allOf", "not", "if", "then", "else", "const", "$ref",
        "$defs", "definitions", "patternProperties", "additionalProperties",
        "$id", "$anchor", "dependencies", "dependentSchemas"};
    for (const char *key : unsupported)
        if (schema.contains(key)) return false;
    if (schema.contains("type") && schema["type"].is_array()) return false;
    if (schema.contains("properties") && schema["properties"].is_object()) {
        for (const auto &entry : schema["properties"].items())
            if (!schema_blocks_openapi_subset(entry.value())) return false;
    }
    if (schema.contains("items") && !schema["items"].is_null() &&
        !schema_blocks_openapi_subset(schema["items"]))
        return false;
    if (schema.contains("anyOf") && schema["anyOf"].is_array()) {
        for (const auto &entry : schema["anyOf"])
            if (!schema_blocks_openapi_subset(entry)) return false;
    }
    return true;
}

const char *schema_type_name(const json &value) {
    if (!value.is_string()) return nullptr;
    const std::string type = ascii_lower(value.get<std::string>());
    if (type == "string") return "STRING";
    if (type == "number") return "NUMBER";
    if (type == "integer") return "INTEGER";
    if (type == "boolean") return "BOOLEAN";
    if (type == "array") return "ARRAY";
    if (type == "object") return "OBJECT";
    if (type == "null") return "NULL";
    return nullptr;
}

json copy_schema_scalars(const json &schema) {
    static constexpr const char *kept[] = {
        "format", "title", "description", "nullable", "enum", "minItems",
        "maxItems", "minProperties", "maxProperties", "minimum", "maximum",
        "minLength", "maxLength", "pattern", "example", "propertyOrdering",
        "default"};
    json out = json::object();
    for (const char *key : kept) {
        if (!schema.contains(key)) continue;
        // Gemini rejects empty enum arrays; they also carry no meaning.
        if (std::string(key) == "enum" && schema[key].is_array() &&
            schema[key].empty())
            continue;
        out[key] = schema[key];
    }
    return out;
}

json to_gemini_schema(const json &schema) {
    if (!schema.is_object()) return json{{"type", "OBJECT"}};
    json out = copy_schema_scalars(schema);
    if (const char *type = schema_type_name(schema.value("type", json())))
        out["type"] = type;
    if (schema.contains("properties") && schema["properties"].is_object()) {
        json properties = json::object();
        for (const auto &entry : schema["properties"].items())
            properties[entry.key()] = to_gemini_schema(entry.value());
        out["properties"] = std::move(properties);
    }
    if (schema.contains("required") && schema["required"].is_array())
        out["required"] = schema["required"];
    if (schema.contains("items") && schema["items"].is_object())
        out["items"] = to_gemini_schema(schema["items"]);
    if (schema.contains("anyOf") && schema["anyOf"].is_array()) {
        json any_of = json::array();
        for (const auto &entry : schema["anyOf"])
            any_of.push_back(to_gemini_schema(entry));
        out["anyOf"] = std::move(any_of);
    }
    return out;
}

/// Effort → Gemini 3 `thinkingLevel`.  Unknown efforts fall back to a dynamic
/// budget instead of inventing a numeric allowance.
const char *thinking_level_for_effort(const std::string &effort) {
    const std::string value = ascii_lower(effort);
    if (value == "minimal") return "MINIMAL";
    if (value == "low") return "LOW";
    if (value == "medium") return "MEDIUM";
    if (value == "high") return "HIGH";
    return nullptr;
}

}  // namespace

std::string bare_model_id(const std::string &model) {
    static const std::string prefix = "models/";
    if (model.size() > prefix.size() &&
        model.compare(0, prefix.size(), prefix) == 0)
        return model.substr(prefix.size());
    return model;
}

bool requires_thought_signature(const std::string &model) {
    // Gemini 3 and later validate the opaque per-part signature when a
    // function call is replayed.  Parse the major version out of the id.
    const std::string bare = bare_model_id(model);
    const std::string marker = "gemini-";
    const auto position = bare.find(marker);
    if (position == std::string::npos) return false;
    const size_t start = position + marker.size();
    int major = 0;
    bool saw_digit = false;
    for (size_t index = start;
         index < bare.size() &&
         std::isdigit(static_cast<unsigned char>(bare[index])); ++index) {
        saw_digit = true;
        major = major * 10 + (bare[index] - '0');
        if (major > 100) break;
    }
    return saw_digit && major >= 3;
}

const char *finish_reason_to_wire(ir::StopReason reason) {
    switch (reason) {
        case ir::StopReason::Stop: return "STOP";
        // Gemini reports a completed tool call as STOP with functionCall parts.
        case ir::StopReason::ToolUse: return "STOP";
        case ir::StopReason::Length: return "MAX_TOKENS";
        case ir::StopReason::ContentFilter: return "SAFETY";
        case ir::StopReason::Unknown:
        case ir::StopReason::Absent:
            break;
    }
    return nullptr;
}

ir::StopReason finish_reason_from_wire(const json &candidate,
                                       bool has_tool_calls) {
    std::string reason;
    if (candidate.is_object() && candidate.contains("finishReason") &&
        candidate["finishReason"].is_string())
        reason = candidate["finishReason"].get<std::string>();
    if (reason.empty() || reason == "FINISH_REASON_UNSPECIFIED")
        return ir::StopReason::Absent;
    if (reason == "STOP")
        return has_tool_calls ? ir::StopReason::ToolUse : ir::StopReason::Stop;
    if (reason == "MAX_TOKENS" || reason == "CONTINUATION")
        return ir::StopReason::Length;
    if (reason == "SAFETY" || reason == "RECITATION" ||
        reason == "BLOCKLIST" || reason == "PROHIBITED_CONTENT" ||
        reason == "SPII" || reason == "IMAGE_SAFETY" ||
        reason == "IMAGE_PROHIBITED_CONTENT")
        return ir::StopReason::ContentFilter;
    // MALFORMED_FUNCTION_CALL, UNEXPECTED_TOOL_CALL, TOO_MANY_TOOL_CALLS,
    // MALFORMED_RESPONSE, MISSING_THOUGHT_SIGNATURE, LANGUAGE, OTHER, ...
    return ir::StopReason::Unknown;
}

void usage_from_wire(const json &usage, ir::Usage &out) {
    if (!usage.is_object()) return;
    // promptTokenCount already includes cached content; tool-use prompt tokens
    // are counted outside it (total = prompt + candidates + tool + thoughts).
    const int prompt = clamped_int(usage, "promptTokenCount");
    const int tool_use = clamped_int(usage, "toolUsePromptTokenCount");
    const int candidates = clamped_int(usage, "candidatesTokenCount");
    const int thoughts = clamped_int(usage, "thoughtsTokenCount");
    out.prompt_tokens = clamped_sum(prompt, tool_use);
    out.cache_read_tokens = clamped_int(usage, "cachedContentTokenCount");
    out.cache_creation_tokens = 0;
    out.completion_tokens = clamped_sum(candidates, thoughts);
    const int total = clamped_int(usage, "totalTokenCount");
    out.total_tokens = total > 0
        ? total : clamped_sum(out.prompt_tokens, out.completion_tokens);
    out.extra = json::object();
    if (thoughts > 0) out.extra["thoughts"] = thoughts;
    if (candidates > 0) out.extra["candidates"] = candidates;
}

json usage_to_wire(const ir::Usage &usage) {
    int thoughts = 0;
    int candidates = 0;
    if (usage.extra.is_object()) {
        thoughts = clamped_int(usage.extra, "thoughts");
        candidates = clamped_int(usage.extra, "candidates");
    }
    if (candidates == 0 && usage.completion_tokens > thoughts)
        candidates = usage.completion_tokens - thoughts;
    json out;
    out["promptTokenCount"] = usage.prompt_tokens;
    out["candidatesTokenCount"] = candidates;
    if (thoughts > 0) out["thoughtsTokenCount"] = thoughts;
    // Gemini's promptTokenCount is cache-inclusive, so the cache read is
    // reported as a subset of prompt rather than an extra bucket.
    if (usage.cache_read_tokens > 0)
        out["cachedContentTokenCount"] =
            std::min(usage.cache_read_tokens, usage.prompt_tokens);
    out["totalTokenCount"] = clamped_sum(usage.prompt_tokens, usage.completion_tokens);
    return out;
}

json generation_config(const ir::ChatRequest &request) {
    json config = json::object();
    if (request.extras.contains("google_generation_config") &&
        request.extras["google_generation_config"].is_object())
        config = request.extras["google_generation_config"];
    if (request.max_tokens.has_value())
        config["maxOutputTokens"] = *request.max_tokens;
    if (request.temperature.has_value())
        config["temperature"] = *request.temperature;
    if (!request.stop_sequences.empty())
        config["stopSequences"] = request.stop_sequences;
    if (request.extras.contains("top_p") && request.extras["top_p"].is_number())
        config["topP"] = request.extras["top_p"];
    if (request.extras.contains("top_k") &&
        request.extras["top_k"].is_number_integer())
        config["topK"] = request.extras["top_k"];

    json thinking = json::object();
    if (request.extras.contains("google_thinking_config") &&
        request.extras["google_thinking_config"].is_object()) {
        thinking = request.extras["google_thinking_config"];
    } else if (request.reasoning.enabled) {
        const std::string effort = ascii_lower(request.reasoning.effort);
        if (request.reasoning.budget_tokens.has_value()) {
            thinking["thinkingBudget"] = *request.reasoning.budget_tokens;
        } else if (effort == "none") {
            thinking.clear();  // explicit "no reasoning"
        } else if (const char *level = thinking_level_for_effort(effort)) {
            thinking["thinkingLevel"] = level;
        } else {
            thinking["thinkingBudget"] = -1;  // dynamic allowance
        }
        if (!thinking.empty()) thinking["includeThoughts"] = true;
    }
    if (!thinking.empty()) config["thinkingConfig"] = thinking;

    const auto &output = request.structured_output;
    if (output.kind == ir::StructuredOutputKind::JsonSchema &&
        output.schema.is_object()) {
        config["responseMimeType"] = "application/json";
        config["responseJsonSchema"] = output.schema;
    } else if (output.kind == ir::StructuredOutputKind::JsonObject) {
        config["responseMimeType"] = "application/json";
    } else if (output.raw.is_object() && output.raw.contains("responseSchema")) {
        // Structured output declared natively on this wire: keep it verbatim.
        config["responseMimeType"] =
            output.raw.value("responseMimeType", "application/json");
        config["responseSchema"] = output.raw["responseSchema"];
    }
    return config;
}

json function_declaration(const ir::Tool &tool) {
    // A declaration that came from this wire round-trips verbatim, including
    // Gemini-only knobs (`response`, `behavior`, `parametersJsonSchema`).
    if (tool.extra.contains("google_declaration") &&
        tool.extra["google_declaration"].is_object()) {
        json decl = tool.extra["google_declaration"];
        decl["name"] = tool.name;
        return decl;
    }
    json decl;
    decl["name"] = tool.name;
    if (!tool.description.empty()) decl["description"] = tool.description;
    const json schema = tool.input_schema.is_object() ? tool.input_schema
                                                      : json::object();
    if (schema.empty()) {
        decl["parameters"] = json{{"type", "OBJECT"}};
        return decl;
    }
    if (schema_blocks_openapi_subset(schema))
        decl["parameters"] = to_gemini_schema(schema);
    else
        decl["parametersJsonSchema"] = schema;
    return decl;
}

json schema_from_wire(const json &schema) {
    if (!schema.is_object()) return json::object();
    json out = json::object();
    for (const auto &entry : schema.items()) {
        if (entry.key() == "type" && entry.value().is_string()) {
            const std::string type = ascii_lower(entry.value().get<std::string>());
            // TYPE_UNSPECIFIED carries no JSON Schema meaning; drop it rather
            // than forward an unknown type to a stricter upstream.
            if (type != "type_unspecified") out["type"] = type;
            continue;
        }
        if (entry.key() == "properties" && entry.value().is_object()) {
            json properties = json::object();
            for (const auto &property : entry.value().items())
                properties[property.key()] = schema_from_wire(property.value());
            out["properties"] = std::move(properties);
            continue;
        }
        if ((entry.key() == "items") && entry.value().is_object()) {
            out["items"] = schema_from_wire(entry.value());
            continue;
        }
        if (entry.key() == "anyOf" && entry.value().is_array()) {
            json any_of = json::array();
            for (const auto &option : entry.value())
                any_of.push_back(schema_from_wire(option));
            out["anyOf"] = std::move(any_of);
            continue;
        }
        out[entry.key()] = entry.value();
    }
    return out;
}

json function_calling_config(const json &tool_choice) {
    if (tool_choice.is_null() || tool_choice.empty()) return json::object();
    if (tool_choice.is_string()) {
        const std::string mode = ascii_lower(tool_choice.get<std::string>());
        if (mode == "auto") return json{{"mode", "AUTO"}};
        if (mode == "none") return json{{"mode", "NONE"}};
        if (mode == "required" || mode == "any")
            return json{{"mode", "ANY"}};
        return json::object();
    }
    if (!tool_choice.is_object()) return json::object();
    if (tool_choice.contains("functionCallingConfig") &&
        tool_choice["functionCallingConfig"].is_object())
        return tool_choice["functionCallingConfig"];
    if (tool_choice.contains("mode") && tool_choice["mode"].is_string())
        return tool_choice;  // already the inner config object
    const std::string type = tool_choice.value("type", "");
    std::string name = tool_choice.value("name", "");
    if (type == "function" && tool_choice.contains("function") &&
        tool_choice["function"].is_object())
        name = tool_choice["function"].value("name", name);
    if (!name.empty())
        return json{{"mode", "ANY"},
                    {"allowedFunctionNames", json::array({name})}};
    if (type == "auto") return json{{"mode", "AUTO"}};
    if (type == "any" || type == "required") return json{{"mode", "ANY"}};
    if (type == "none") return json{{"mode", "NONE"}};
    return json::object();
}

std::string client_tool_name(const ir::ConversionContext *context,
                             const std::string &upstream_name) {
    if (!context) return upstream_name;
    for (const auto &mapping : context->tools.mappings)
        if (mapping.flat_name == upstream_name) return mapping.original_name;
    return upstream_name;
}

std::map<std::string, std::string> tool_call_names(
    const std::vector<ir::Message> &items) {
    std::map<std::string, std::string> names;
    for (const auto &item : items) {
        for (const auto &block : item.content) {
            if (block.kind != ir::ContentKind::ToolUse) continue;
            if (block.tool_call_id.empty() || block.tool_name.empty()) continue;
            names[block.tool_call_id] = block.tool_name;
        }
    }
    return names;
}

}  // namespace gfmt
