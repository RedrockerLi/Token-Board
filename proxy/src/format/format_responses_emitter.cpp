#include "format_responses_emitter.h"
#include "reasoning_bridge.h"

#include <algorithm>

using namespace ir;

ResponsesStreamEmitter::ResponsesStreamEmitter(const ConversionContext *ctx)
    : context_(ctx), id_(ctx && !ctx->generated_response_id.empty()
                              ? ctx->generated_response_id
                              : fmt::generate_response_id()) {}

bool ResponsesStreamEmitter::emit(const StreamEvent &ev, const Sink &sink) {
    switch (ev.type) {
        case StreamEventType::MessageStart:
            if (!context_ || context_->generated_response_id.empty())
                id_ = ev.extra.value("id", id_);
            model_ = ev.extra.value("model", model_);
            return started_ || emit_response_created(sink);
        case StreamEventType::ContentTextDelta: {
            if (!started_ && !emit_response_created(sink)) return false;
            const int oi = out_index_for(ev.index, kTextStream);
            if (ev.item.extra.contains("raw_item") &&
                ev.item.extra["raw_item"].is_object())
                raw_items_[oi] = ev.item.extra["raw_item"];
            if (!text_started_.count(oi) && !emit_text_item_start(sink, oi, ev)) return false;
            json d{{"type", "response.output_text.delta"},
                   {"item_id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                   {"output_index", oi}, {"content_index", 0}, {"delta", ev.text}};
            if (!sink(frame("response.output_text.delta", d))) return false;
            text_[oi] += ev.text;
            return true;
        }
        case StreamEventType::ContentThinkingDelta: {
            if (!started_ && !emit_response_created(sink)) return false;
            const int oi = out_index_for(ev.index, kReasoningStream);
            item_kind_[oi] = 2;
            if (ev.extra.contains("reasoning_signature") &&
                ev.extra["reasoning_signature"].is_string())
                reasoning_signatures_[oi] = ev.extra["reasoning_signature"];
            if (ev.extra.contains("reasoning_redacted_data") &&
                ev.extra["reasoning_redacted_data"].is_string())
                reasoning_redacted_data_[oi] = ev.extra["reasoning_redacted_data"];
            if (ev.item.extra.contains("raw_item") &&
                ev.item.extra["raw_item"].is_object())
                raw_items_[oi] = ev.item.extra["raw_item"];
            if (ev.text.empty()) {
                if (!text_started_.count(oi) &&
                    (reasoning_signatures_.count(oi) ||
                     reasoning_redacted_data_.count(oi) ||
                     ev.item.extra.contains("raw_item")))
                    return emit_reasoning_item_start(sink, oi, ev);
                return true;
            }
            if (!text_started_.count(oi) && !emit_reasoning_item_start(sink, oi, ev)) return false;
            json d{{"type", "response.reasoning_summary_text.delta"},
                   {"item_id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                   {"output_index", oi}, {"summary_index", 0}, {"delta", ev.text}};
            if (!sink(frame("response.reasoning_summary_text.delta", d))) return false;
            reasoning_text_[oi] += ev.text;
            return true;
        }
        case StreamEventType::ToolCallStart: {
            if (!started_ && !emit_response_created(sink)) return false;
            const bool typed_start = ev.item.extra.contains("raw_item") &&
                (ev.item.item_kind == ItemKind::Message ||
                 ev.item.item_kind == ItemKind::Reasoning);
            const int start_kind = typed_start && ev.item.item_kind == ItemKind::Message
                ? kTextStream : typed_start && ev.item.item_kind == ItemKind::Reasoning
                    ? kReasoningStream : kToolStream;
            const int oi = out_index_for(ev.index, start_kind);
            const bool is_typed_message = ev.item.extra.contains("raw_item") &&
                ev.item.item_kind == ItemKind::Message;
            const bool is_typed_reasoning = ev.item.extra.contains("raw_item") &&
                ev.item.item_kind == ItemKind::Reasoning;
            if (is_typed_message)
                return text_started_.count(oi) ||
                    emit_text_item_start(sink, oi, ev);
            if (is_typed_reasoning)
                return text_started_.count(oi) ||
                    emit_reasoning_item_start(sink, oi, ev);
            if (ev.item.extra.contains("raw_item") &&
                ev.item.item_kind == ItemKind::Opaque) {
                item_kind_[oi] = 3;
                raw_items_[oi] = ev.item.extra["raw_item"];
                item_ids_[oi] = ev.item_id.empty()
                    ? raw_items_[oi].value("id", item_id(oi)) : ev.item_id;
                json item = raw_items_[oi];
                item["id"] = item_ids_[oi];
                return sink(frame("response.output_item.added", json{
                    {"type", "response.output_item.added"},
                    {"output_index", oi}, {"item", item}}));
            }
            if (ev.item.extra.contains("raw_item") &&
                ev.item.extra["raw_item"].is_object())
                raw_items_[oi] = ev.item.extra["raw_item"];
            item_kind_[oi] = 1;
            std::string kind = ev.extra.value("tool_kind", "function_call");
            std::string name = ev.arguments;
            std::string namespace_name;
            if (context_) {
                for (const auto &mapping : context_->tools.mappings) {
                    if (mapping.flat_name != name) continue;
                    if (mapping.kind == ToolKind::Custom) kind = "custom_tool_call";
                    else if (mapping.kind == ToolKind::ToolSearch) kind = "tool_search_call";
                    name = mapping.original_name;
                    namespace_name = mapping.namespace_name;
                    break;
                }
            }
            item_ids_[oi] = ev.item_id.empty() ? item_id(oi) : ev.item_id;
            const std::string call_id = ev.text.empty() ? fmt::generate_call_id() : ev.text;
            tools_[oi] = {call_id, name, "", kind, item_ids_[oi], namespace_name};
            json item{{"type", kind}, {"id", item_ids_[oi]}, {"call_id", call_id},
                      {"name", name}, {"status", ev.item.status.empty()
                          ? "in_progress" : ev.item.status}};
            if (!ev.item.phase.empty()) item["phase"] = ev.item.phase;
            if (kind == "custom_tool_call") item["input"] = "";
            else if (kind == "tool_search_call") {
                item["arguments"] = json::object();
                item["execution"] = "client";
            }
            else item["arguments"] = "";
            if (!namespace_name.empty()) item["namespace"] = namespace_name;
            return sink(frame("response.output_item.added",
                              json{{"type", "response.output_item.added"},
                                   {"output_index", oi}, {"item", item}}));
        }
        case StreamEventType::ToolCallArgumentDelta: {
            const int oi = out_index_for(ev.index, kToolStream);
            auto &tool = tools_[oi];
            tool.arguments += ev.arguments;
            if (tool.kind == "tool_search_call")
                // tool_search has an Item-level payload in Responses; it does
                // not use function-argument delta events.
                return true;
            if (tool.kind == "custom_tool_call" && context_ &&
                context_->target == ApiFormat::OpenAIResponses)
                // Compatibility Chat/Anthropic functions stream the wrapper
                // JSON.  Emit the native custom input only once it can be
                // decoded at Item completion.
                return true;
            const std::string event_name = tool.kind == "custom_tool_call"
                ? "response.custom_tool_call_input.delta"
                : "response.function_call_arguments.delta";
            json d{{"type", event_name},
                   {"item_id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                   {"output_index", oi}, {"delta", ev.arguments}};
            return sink(frame(event_name, d));
        }
        case StreamEventType::ToolCallDone: {
            const bool typed_done = ev.item.extra.contains("raw_item") &&
                (ev.item.item_kind == ItemKind::Message ||
                 ev.item.item_kind == ItemKind::Reasoning);
            const int done_kind = typed_done && ev.item.item_kind == ItemKind::Message
                ? kTextStream : typed_done && ev.item.item_kind == ItemKind::Reasoning
                    ? kReasoningStream : kToolStream;
            const int oi = out_index_for(ev.index, done_kind);
            const bool is_typed_item = ev.item.extra.contains("raw_item") &&
                (ev.item.item_kind == ItemKind::Message ||
                 ev.item.item_kind == ItemKind::Reasoning);
            if (ev.item.extra.contains("raw_item") &&
                ev.item.extra["raw_item"].is_object())
                raw_items_[oi] = ev.item.extra["raw_item"];
            if (is_typed_item)
                return close_item(sink, oi);
            if (ev.item.extra.contains("raw_item") &&
                ev.item.item_kind == ItemKind::Opaque) {
                json item = raw_items_[oi];
                item["id"] = item_ids_.count(oi) ? item_ids_[oi]
                                                  : item.value("id", item_id(oi));
                text_finished_.insert(oi);
                return sink(frame("response.output_item.done", json{
                    {"type", "response.output_item.done"},
                    {"output_index", oi}, {"item", item}}));
            }
            auto &tool = tools_[oi];
            if (tool.call_id.empty()) tool.call_id = fmt::generate_call_id();
            std::string payload = ev.arguments.empty() ? tool.arguments : ev.arguments;
            tool.arguments = payload;
            json item{{"type", tool.kind},
                      {"id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                      {"call_id", tool.call_id}, {"name", tool.name},
                      {"status", ev.item.status.empty() ? "completed" : ev.item.status}};
            if (!ev.item.phase.empty()) item["phase"] = ev.item.phase;
            if (!tool.namespace_name.empty()) item["namespace"] = tool.namespace_name;
            std::string custom_input = payload;
            if (tool.kind == "custom_tool_call" && context_ &&
                context_->target == ApiFormat::OpenAIResponses) {
                try {
                    const json parsed = json::parse(payload);
                    if (parsed.is_object() && parsed.contains("input") &&
                        parsed["input"].is_string())
                        custom_input = parsed["input"].get<std::string>();
                } catch (...) {
                    // Preserve the raw payload; the upstream malformed
                    // argument will be rejected by the next request parser.
                }
                if (!custom_input.empty()) {
                    if (!sink(frame("response.custom_tool_call_input.delta",
                                    json{{"type", "response.custom_tool_call_input.delta"},
                                         {"item_id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                                         {"output_index", oi}, {"delta", custom_input}})))
                        return false;
                }
                payload = custom_input;
            }
            if (tool.kind == "custom_tool_call") item["input"] = payload;
            else if (tool.kind == "tool_search_call") {
                try { item["arguments"] = json::parse(payload); }
                catch (...) { item["arguments"] = payload; }
                item["execution"] = "client";
            } else item["arguments"] = payload;
            return sink(frame("response.output_item.done",
                              json{{"type", "response.output_item.done"},
                                   {"output_index", oi}, {"item", item}}));
        }
        case StreamEventType::MessageFinish:
            if (finished_) return true;
            if (!close_open_items(sink)) return false;
            deferred_status_ = fmt::stop_reason_to_responses(ev.stop_reason);
            return true;
        case StreamEventType::UsageEvent:
            last_usage_ = ev.usage;
            if (!finished_ && !deferred_status_.empty() && !completed_emitted_)
                return emit_completed(sink);
            return true;
        case StreamEventType::ErrorEvent: {
            finished_ = true;
            json err = ev.extra.contains("error") ? ev.extra["error"]
                : json{{"message", ev.extra.value("message", "upstream error")}};
            json response{{"id", id_}, {"object", "response"}, {"status", "failed"},
                          {"error", err}};
            return sink(frame("response.failed", json{{"type", "response.failed"},
                                                        {"response", response}}));
        }
    }
    return true;
}

bool ResponsesStreamEmitter::finish(const Sink &sink) {
    if (!started_) return true;
    if (!finished_ && !completed_emitted_) return emit_completed(sink);
    return true;
}

std::string ResponsesStreamEmitter::item_id(int index) const {
    return id_ + "_" + std::to_string(index);
}

std::string ResponsesStreamEmitter::frame(const std::string &event, const json &payload) {
    return "event: " + event + "\ndata: " + payload.dump() + "\n\n";
}

int ResponsesStreamEmitter::out_index_for(int ir_index, int stream_kind) {
    const auto key = std::make_pair(stream_kind, ir_index);
    auto it = out_index_.find(key);
    if (it != out_index_.end()) return it->second;
    // A Responses upstream already gives the canonical output index. Preserve
    // it when adapting namespace/custom/tool_search items so a later delta
    // cannot reorder items merely because another stream kind arrived first.
    const bool preserve_source_index =
        context_ && context_->source == ApiFormat::OpenAIResponses;
    const int oi = preserve_source_index && ir_index >= 0
        ? ir_index : next_output_index_++;
    if (preserve_source_index)
        next_output_index_ = std::max(next_output_index_, oi + 1);
    out_index_[key] = oi;
    return oi;
}

bool ResponsesStreamEmitter::emit_response_created(const Sink &sink) {
    started_ = true;
    json response{{"id", id_}, {"object", "response"}, {"status", "in_progress"},
                  {"model", model_}, {"output", json::array()}, {"usage", nullptr}};
    if (!sink(frame("response.created", json{{"type", "response.created"},
                                               {"response", response}}))) return false;
    return sink(frame("response.in_progress", json{{"type", "response.in_progress"},
                                                     {"response", response}}));
}

bool ResponsesStreamEmitter::emit_text_item_start(const Sink &sink, int index,
                                                  const StreamEvent &ev) {
    text_started_.insert(index); item_kind_[index] = 0;
    if (!item_ids_.count(index))
        item_ids_[index] = ev.item_id.empty() ? item_id(index) : ev.item_id;
    json item = raw_items_.count(index) ? raw_items_[index] : json::object();
    item["id"] = item_ids_[index];
    item["type"] = "message";
    item["role"] = "assistant";
    item["status"] = ev.item.status.empty() ? "in_progress" : ev.item.status;
    if (!item.contains("content")) item["content"] = json::array();
    if (!ev.item.phase.empty()) item["phase"] = ev.item.phase;
    if (!sink(frame("response.output_item.added", json{{"type", "response.output_item.added"},
                                                         {"output_index", index}, {"item", item}}))) return false;
    json part{{"type", "output_text"}, {"text", ""}, {"annotations", json::array()}};
    return sink(frame("response.content_part.added", json{{"type", "response.content_part.added"},
                                                            {"item_id", item_ids_[index]},
                                                            {"output_index", index}, {"content_index", 0},
                                                            {"part", part}}));
}

bool ResponsesStreamEmitter::emit_reasoning_item_start(const Sink &sink, int index,
                                                       const StreamEvent &ev) {
    text_started_.insert(index); item_kind_[index] = 2;
    if (!item_ids_.count(index))
        item_ids_[index] = ev.item_id.empty() ? item_id(index) : ev.item_id;
    json item = raw_items_.count(index) ? raw_items_[index] : json::object();
    item["id"] = item_ids_[index];
    item["type"] = "reasoning";
    item["status"] = ev.item.status.empty() ? "in_progress" : ev.item.status;
    if (!item.contains("summary")) item["summary"] = json::array();
    if (!ev.item.phase.empty()) item["phase"] = ev.item.phase;
    if (!sink(frame("response.output_item.added", json{{"type", "response.output_item.added"},
                                                         {"output_index", index}, {"item", item}}))) return false;
    json part{{"type", "summary_text"}, {"text", ""}};
    return sink(frame("response.reasoning_summary_part.added",
                      json{{"type", "response.reasoning_summary_part.added"},
                           {"item_id", item_ids_[index]}, {"output_index", index},
                           {"summary_index", 0}, {"part", part}}));
}

bool ResponsesStreamEmitter::close_item(const Sink &sink, int index) {
    if (text_finished_.count(index)) return true;
    const int kind = item_kind_.count(index) ? item_kind_[index] : 0;
    const std::string iid = item_ids_.count(index) ? item_ids_[index] : item_id(index);
    if (kind == 3) {
        json item = raw_items_.count(index) ? raw_items_[index] : json::object();
        item["id"] = iid;
        item["status"] = "completed";
        if (!sink(frame("response.output_item.done", json{
                {"type", "response.output_item.done"},
                {"output_index", index}, {"item", item}}))) return false;
        text_finished_.insert(index);
        return true;
    }
    if (kind == 0) {
        if (!sink(frame("response.output_text.done", json{
                {"type", "response.output_text.done"}, {"item_id", iid},
                {"output_index", index}, {"content_index", 0},
                {"text", text_[index]}}))) return false;
        json part{{"type", "output_text"}, {"text", text_[index]},
                  {"annotations", json::array()}};
        if (!sink(frame("response.content_part.done", json{
                {"type", "response.content_part.done"}, {"item_id", iid},
                {"output_index", index}, {"content_index", 0}, {"part", part}})))
            return false;
    } else if (kind == 2) {
        if (!sink(frame("response.reasoning_summary_text.done", json{
                {"type", "response.reasoning_summary_text.done"}, {"item_id", iid},
                {"output_index", index}, {"summary_index", 0},
                {"text", reasoning_text_[index]}}))) return false;
        json part{{"type", "summary_text"}, {"text", reasoning_text_[index]}};
        if (!sink(frame("response.reasoning_summary_part.done", json{
                {"type", "response.reasoning_summary_part.done"}, {"item_id", iid},
                {"output_index", index}, {"summary_index", 0}, {"part", part}})))
            return false;
    }
    json item = raw_items_.count(index) ? raw_items_[index] : json::object();
    item["id"] = iid;
    item["type"] = kind == 2 ? "reasoning" : "message";
    item["status"] = "completed";
    if (kind == 2) {
        item["summary"] = json::array({json{{"type", "summary_text"},
                                              {"text", reasoning_text_[index]}}});
        if (reasoning_redacted_data_.count(index)) {
            const json restored = fmt::responses_reasoning_from_anthropic_block(
                json{{"type", "redacted_thinking"},
                     {"data", reasoning_redacted_data_[index]}});
            if (restored.contains("encrypted_content"))
                item["encrypted_content"] = restored["encrypted_content"];
        } else if (reasoning_signatures_.count(index)) {
            const json restored = fmt::responses_reasoning_from_anthropic_block(
                json{{"type", "thinking"}, {"thinking", reasoning_text_[index]},
                     {"signature", reasoning_signatures_[index]}});
            if (restored.contains("encrypted_content"))
                item["encrypted_content"] = restored["encrypted_content"];
        }
    } else {
        item["role"] = "assistant";
        item["content"] = json::array({json{{"type", "output_text"},
                                             {"text", text_[index]},
                                             {"annotations", json::array()}}});
    }
    if (!sink(frame("response.output_item.done", json{
            {"type", "response.output_item.done"}, {"output_index", index},
            {"item", item}}))) return false;
    text_finished_.insert(index);
    return true;
}

bool ResponsesStreamEmitter::close_open_items(const Sink &sink) {
    std::set<int> ordered_indexes;
    for (const auto &entry : out_index_) ordered_indexes.insert(entry.second);
    for (const int oi : ordered_indexes) {
        if (text_finished_.count(oi)) continue;
        const int kind = item_kind_.count(oi) ? item_kind_[oi] : 0;
        if (kind == 1) {
            auto &tool = tools_[oi];
            if (tool.call_id.empty()) tool.call_id = fmt::generate_call_id();
            json item{{"type", tool.kind.empty() ? "function_call" : tool.kind},
                      {"id", item_ids_.count(oi) ? item_ids_[oi] : item_id(oi)},
                      {"call_id", tool.call_id}, {"name", tool.name},
                      {"status", "incomplete"}, {"arguments", tool.arguments}};
            if (tool.kind == "custom_tool_call") {
                item.erase("arguments"); item["input"] = tool.arguments;
            } else if (tool.kind == "tool_search_call") {
                try { item["arguments"] = json::parse(tool.arguments); }
                catch (...) { item["arguments"] = json::object(); }
                item["execution"] = "client";
            }
            if (!tool.namespace_name.empty()) item["namespace"] = tool.namespace_name;
            if (!sink(frame("response.output_item.done", json{
                    {"type", "response.output_item.done"},
                    {"output_index", oi}, {"item", item}}))) return false;
            text_finished_.insert(oi);
            continue;
        }
        if (!close_item(sink, oi)) return false;
    }
    return true;
}

json ResponsesStreamEmitter::build_output() {
    json output = json::array();
    std::set<int> indexes;
    for (const auto &entry : out_index_) indexes.insert(entry.second);
    for (int idx : indexes) {
        const int kind = item_kind_.count(idx) ? item_kind_[idx] : 0;
        const std::string iid = item_ids_.count(idx) ? item_ids_[idx] : item_id(idx);
        if (kind == 1) {
            auto &tool = tools_[idx];
            json item = raw_items_.count(idx) ? raw_items_[idx] : json::object();
            item["id"] = iid;
            item["type"] = tool.kind.empty() ? "function_call" : tool.kind;
            item["call_id"] = tool.call_id;
            item["name"] = tool.name;
            item["status"] = "completed";
            if (!tool.namespace_name.empty()) item["namespace"] = tool.namespace_name;
            if (tool.kind == "custom_tool_call") item["input"] = tool.arguments;
            else if (tool.kind == "tool_search_call") {
                try { item["arguments"] = json::parse(tool.arguments); }
                catch (...) { item["arguments"] = tool.arguments; }
                item["execution"] = "client";
            } else item["arguments"] = tool.arguments;
            output.push_back(std::move(item));
        } else if (kind == 3) {
            json item = raw_items_.count(idx) ? raw_items_[idx] : json::object();
            if (!item.contains("id")) item["id"] = iid;
            output.push_back(std::move(item));
        } else if (kind == 2) {
            json item = raw_items_.count(idx) ? raw_items_[idx] : json::object();
            item["id"] = iid;
            item["type"] = "reasoning";
            item["summary"] = json::array({json{{"type", "summary_text"},
                                                   {"text", reasoning_text_[idx]}}});
            if (reasoning_redacted_data_.count(idx)) {
                const json restored = fmt::responses_reasoning_from_anthropic_block(
                    json{{"type", "redacted_thinking"},
                         {"data", reasoning_redacted_data_[idx]}});
                if (restored.contains("encrypted_content"))
                    item["encrypted_content"] = restored["encrypted_content"];
            } else if (reasoning_signatures_.count(idx)) {
                const json restored = fmt::responses_reasoning_from_anthropic_block(
                    json{{"type", "thinking"}, {"thinking", reasoning_text_[idx]},
                         {"signature", reasoning_signatures_[idx]}});
                if (restored.contains("encrypted_content"))
                    item["encrypted_content"] = restored["encrypted_content"];
            }
            output.push_back(std::move(item));
        } else {
            json item = raw_items_.count(idx) ? raw_items_[idx] : json::object();
            item["id"] = iid;
            item["type"] = "message";
            item["status"] = "completed";
            item["role"] = "assistant";
            item["content"] = json::array({json{{"type", "output_text"},
                                                  {"text", text_[idx]},
                                                  {"annotations", json::array()}}});
            output.push_back(std::move(item));
        }
    }
    return output;
}

bool ResponsesStreamEmitter::emit_completed(const Sink &sink) {
    completed_emitted_ = true;
    json response{{"id", id_}, {"object", "response"}, {"status", deferred_status_},
                  {"model", model_}, {"output", build_output()},
                  {"usage", json{{"input_tokens", last_usage_.prompt_tokens},
                                  {"output_tokens", last_usage_.completion_tokens},
                                  {"total_tokens", last_usage_.total_tokens}}},
                  {"error", nullptr}, {"incomplete_details", nullptr}};
    const std::string event = deferred_status_ == "incomplete" ? "response.incomplete" : "response.completed";
    return sink(frame(event, json{{"type", event}, {"response", response}}));
}
