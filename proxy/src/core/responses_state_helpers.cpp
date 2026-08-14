#include "proxy_server_internal.h"

namespace {
std::vector<json> input_items(const json &body) {
    if (!body.contains("input")) return {};
    const auto &input = body["input"];
    if (input.is_array()) return input.get<std::vector<json>>();
    if (input.is_string()) {
        return {json{{"type", "message"}, {"role", "user"},
                     {"content", json::array({json{{"type", "input_text"},
                                                     {"text", input.get<std::string>()}}})}}};
    }
    return {};
}

} // namespace

bool has_raw_content(const std::vector<ir::ContentBlock> &blocks) {
    for (const auto &block : blocks) {
        if (block.extra.contains("raw") && block.extra["raw"].is_object())
            return true;
        if (has_raw_content(block.nested)) return true;
    }
    return false;
}

bool expand_responses_state(ProxyServer &server, CodecRegistry &codecs,
                            RequestContext &context, std::string &error) {
    if (context.client_format != ir::ApiFormat::OpenAIResponses ||
        context.previous_response_id.empty()) return true;
    std::vector<json> history;
    if (!server.responses_state().lookup(context.previous_response_id, history)) {
        error = "previous_response_id was not found in the proxy state cache";
        return false;
    }
    // Keep the caller's input separate from the expanded wire request.  The
    // latter is needed by upstream conversion; the former is what belongs in
    // the new response record after the parent chain has been replayed.
    context.state_current_input = input_items(context.parsed_json);
    json expanded = context.parsed_json;
    json combined = json::array();
    for (const auto &item : history) combined.push_back(item);
    for (const auto &item : input_items(context.parsed_json)) combined.push_back(item);
    expanded["input"] = std::move(combined);
    expanded.erase("previous_response_id");
    ir::ChatRequest parsed;
    try {
        if (!codecs.get(ir::ApiFormat::OpenAIResponses).parse_request(
                expanded, parsed, error)) return false;
    } catch (const std::exception &e) {
        error = e.what();
        return false;
    } catch (...) {
        error = "invalid expanded Responses state";
        return false;
    }
    context.parsed_json = std::move(expanded);
    context.raw_body = std::make_shared<const std::string>(context.parsed_json.dump());
    context.parsed_ir = std::move(parsed);
    context.ir_ready = true;
    context.state_expanded = true;
    return true;
}

bool target_supports_request(ir::ApiFormat target, ir::ApiFormat harness,
                             const ir::ChatRequest &request,
                             std::string &reason) {
    const auto failures = request_feature_failures(target, harness, request);
    if (!failures.empty()) { reason = failures.front().reason; return false; }
    return true;
}

std::vector<RequestFeatureFailure> request_feature_failures(
    ir::ApiFormat target, ir::ApiFormat harness,
    const ir::ChatRequest &request) {
    std::vector<RequestFeatureFailure> failures;
    auto add = [&](const std::string &feature, const std::string &why) {
        failures.push_back({feature, why});
    };
    if (target != harness) {
        const auto &items = request.items.empty() ? request.messages : request.items;
        for (const auto &item : items)
            if (has_raw_content(item.content))
                add("unknown_content", "unknown content blocks cannot be converted losslessly");
        if (has_raw_content(request.system))
            add("unknown_system_content", "unknown system content blocks cannot be converted losslessly");
    }
    if (request.structured_output.raw.is_object() &&
        !request.structured_output.raw.empty()) {
        const std::string type = request.structured_output.raw.value("type", "text");
        if (type != "text" && type != "json_object" && type != "json_schema") {
            add("structured_output", "unknown structured output format cannot be converted");
        }
    }
    if (target == ir::ApiFormat::Anthropic &&
        request.structured_output.kind != ir::StructuredOutputKind::Text) {
        add("structured_output", "structured JSON output is not representable by Anthropic Messages");
    }
    for (const char *key : {"conversation", "background", "prompt", "context_management"}) {
        if (target != ir::ApiFormat::OpenAIResponses && request.extras.contains(key)) {
            add(key, std::string("Responses ") + key + " cannot be represented by the target protocol");
        }
    }
    if (request.extras.contains("text") && request.extras["text"].is_object()) {
        const auto &text = request.extras["text"];
        for (const auto &entry : text.items()) {
            if (target != ir::ApiFormat::OpenAIResponses && entry.key() != "format") {
                add("text." + entry.key(), "unknown Responses text control cannot be converted");
            }
        }
    }
    for (const auto &tool : request.tools) {
        if (tool.kind == ir::ToolKind::Hosted &&
            target != ir::ApiFormat::OpenAIResponses) {
            add("hosted_tool", "hosted Responses tools cannot be converted to a client tool");
        }
        if (tool.kind == ir::ToolKind::Namespace &&
            target != ir::ApiFormat::OpenAIResponses) {
            bool convertible = !tool.children.empty();
            for (const auto &child : tool.children) {
                convertible = convertible &&
                    (child.kind == ir::ToolKind::Function ||
                     child.kind == ir::ToolKind::Custom);
                if (child.kind == ir::ToolKind::Hosted)
                    add("hosted_tool", "hosted namespace child cannot be converted to a client tool");
            }
            if (!convertible)
                add("namespace_tool", "namespace tool has no lossless client-tool representation");
        }
    }
    if (target != ir::ApiFormat::OpenAIResponses &&
        request.tool_choice.is_object() &&
        request.tool_choice.value("type", "") == "namespace") {
        add("namespace_tool_choice", "namespace tool_choice cannot be represented by the target protocol");
    }
    if (target == harness && harness == ir::ApiFormat::OpenAIResponses &&
        responses_request_needs_tool_adapter(request) &&
        request.tool_choice.is_object() &&
        request.tool_choice.value("type", "") == "namespace") {
        add("namespace_tool_choice", "namespace tool_choice cannot be represented after namespace flattening");
    }
    for (const auto &item : request.items) {
        if (item.item_kind == ir::ItemKind::Opaque &&
            target != ir::ApiFormat::OpenAIResponses) {
            add("opaque_item", "opaque Responses Items require a Responses-capable upstream");
        }
    }
    return failures;
}

bool responses_request_needs_tool_adapter(const ir::ChatRequest &request) {
    for (const auto &tool : request.tools) {
        if (tool.kind == ir::ToolKind::ToolSearch) return true;
        if (tool.kind == ir::ToolKind::Namespace) {
            bool convertible = true;
            for (const auto &child : tool.children)
                convertible = convertible &&
                    (child.kind == ir::ToolKind::Function ||
                     child.kind == ir::ToolKind::Custom);
            if (!tool.children.empty() && convertible) return true;
        }
    }
    return false;
}

void record_responses_state(ProxyServer &server, const json &request_body,
                            const std::string &response_body,
                            const std::vector<json> *current_input) {
    json response;
    try { response = json::parse(response_body); } catch (...) { return; }
    if (!response.is_object()) return;
    const std::string id = response.value("id", "");
    const std::string status = response.value("status", "");
    if (id.empty() || (status != "completed" && status != "incomplete")) return;
    std::vector<json> output;
    if (response.contains("output") && response["output"].is_array())
        output = response["output"].get<std::vector<json>>();
    if (current_input)
        server.responses_state().record(id, *current_input, output);
    else
        server.responses_state().record(id, input_items(request_body), output);
}
