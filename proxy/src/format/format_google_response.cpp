#include "format_google_internal.h"

using namespace ir;

namespace {

void build_output_items(ChatResponse &out) {
    AgentItem message;
    message.role = "assistant";
    message.item_kind = ItemKind::Message;
    message.group_id = 1;
    bool open = false;
    auto flush = [&]() {
        if (!open) return;
        out.output_items.push_back(message);
        message = AgentItem{};
        message.role = "assistant";
        message.item_kind = ItemKind::Message;
        message.group_id = 1;
        open = false;
    };
    for (const auto &block : out.content) {
        if (block.kind == ContentKind::ToolUse) {
            flush();
            AgentItem call;
            call.item_kind = ItemKind::FunctionCall;
            call.role = "assistant";
            call.group_id = 1;
            call.call_id = block.tool_call_id;
            call.name = block.tool_name;
            call.payload = block.tool_input.dump();
            out.output_items.push_back(std::move(call));
            continue;
        }
        message.content.push_back(block);
        open = true;
    }
    flush();
}

void copy_candidate_extras(const json &candidate, ChatResponse &out) {
    for (const char *key : {"safetyRatings", "groundingMetadata",
                            "citationMetadata", "urlContextMetadata",
                            "logprobsResult", "avgLogprobs"})
        if (candidate.contains(key)) out.extras[key] = candidate[key];
    if (candidate.contains("finishMessage") &&
        candidate["finishMessage"].is_string())
        out.extras["finish_message"] = candidate["finishMessage"];
}

}  // namespace

bool GoogleCodec::parse_response(const json &in, ir::ChatResponse &out,
                                 std::string &err,
                                 const ir::ConversionContext *context) const {
    if (!in.is_object()) {
        err = "Gemini response must be a JSON object";
        return false;
    }
    if (in.contains("responseId") && in["responseId"].is_string())
        out.id = in["responseId"].get<std::string>();
    if (in.contains("modelVersion") && in["modelVersion"].is_string())
        out.model = in["modelVersion"].get<std::string>();
    if (in.contains("usageMetadata"))
        gfmt::usage_from_wire(in["usageMetadata"], out.usage);
    if (in.contains("promptFeedback") && in["promptFeedback"].is_object())
        out.extras["prompt_feedback"] = in["promptFeedback"];

    const json *candidate = nullptr;
    if (in.contains("candidates") && in["candidates"].is_array() &&
        !in["candidates"].empty())
        candidate = &in["candidates"][0];
    if (candidate == nullptr) {
        // A blocked prompt answers with promptFeedback and no candidates at
        // all; that is a refusal, not an empty successful answer.
        const bool blocked = in.contains("promptFeedback") &&
            in["promptFeedback"].is_object() &&
            in["promptFeedback"].contains("blockReason");
        out.stop_reason = blocked ? StopReason::ContentFilter
                                  : StopReason::Absent;
        return true;
    }
    if (!candidate->is_object()) {
        err = "Gemini candidate must be a JSON object";
        return false;
    }

    if (candidate->contains("content") &&
        (*candidate)["content"].is_object() &&
        (*candidate)["content"].contains("parts"))
        gfmt::blocks_from_parts((*candidate)["content"]["parts"], out.content);

    bool has_tool_calls = false;
    for (auto &block : out.content) {
        if (block.kind != ContentKind::ToolUse) continue;
        has_tool_calls = true;
        block.tool_name = gfmt::client_tool_name(context, block.tool_name);
    }
    out.stop_reason = gfmt::finish_reason_from_wire(*candidate, has_tool_calls);
    copy_candidate_extras(*candidate, out);
    if (in["candidates"].size() > 1)
        out.extras["extra_candidates"] = in["candidates"].size() - 1;
    build_output_items(out);
    return true;
}

json GoogleCodec::serialize_response(
    const ir::ChatResponse &in, const ir::ConversionContext * /*context*/) const {
    json candidate = json::object();
    candidate["index"] = 0;
    json content;
    content["role"] = "model";
    content["parts"] = gfmt::parts_from_blocks(in.content, false);
    candidate["content"] = std::move(content);
    if (const char *reason = gfmt::finish_reason_to_wire(in.stop_reason))
        candidate["finishReason"] = reason;

    json body;
    body["candidates"] = json::array({candidate});
    if (!in.id.empty()) body["responseId"] = in.id;
    if (!in.model.empty()) body["modelVersion"] = in.model;
    if (in.usage.prompt_tokens > 0 || in.usage.completion_tokens > 0 ||
        in.usage.cache_read_tokens > 0)
        body["usageMetadata"] = gfmt::usage_to_wire(in.usage);
    if (in.extras.contains("prompt_feedback"))
        body["promptFeedback"] = in.extras["prompt_feedback"];
    json &out_candidate = body["candidates"][0];
    for (const char *key : {"safetyRatings", "groundingMetadata",
                            "citationMetadata", "urlContextMetadata",
                            "logprobsResult", "avgLogprobs"})
        if (in.extras.contains(key)) out_candidate[key] = in.extras[key];
    if (in.extras.contains("finish_message"))
        out_candidate["finishMessage"] = in.extras["finish_message"];
    return body;
}
