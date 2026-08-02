#include "format_responses.h"

#include <cstring>
#include <set>

#include "format_common.h"

using namespace ir;

namespace {

const char *kConsumed[] = {
    "model", "instructions", "input", "tools", "tool_choice", "reasoning",
    "max_output_tokens", "temperature", "stream", "previous_response_id",
    "store", "include", "metadata", "user", "text", "parallel_tool_calls",
};

// Try to parse a JSON string into an object; fall back to empty object.
json parse_arguments_string(const std::string &args) {
    try {
        json j = json::parse(args);
        return j.is_object() ? j : json::object();
    } catch (...) {
        return json::object();
    }
}

class ResponsesCodec : public FormatCodec {
public:
    ResponsesCodec() : FormatCodec(ir::ApiFormat::OpenAIResponses) {}

    bool parse_request(const json &in, ir::ChatRequest &out,
                       std::string &err) const override;
    json serialize_request(const ir::ChatRequest &in) const override;
    bool parse_response(const json &in, ir::ChatResponse &out,
                        std::string &err) const override;
    json serialize_response(const ir::ChatResponse &in) const override;
    json parse_error_body(const json &upstream_err) const override {
        return fmt::normalize_error_body(upstream_err);
    }
    json serialize_error_body(const json &normalized) const override {
        json out;
        out["error"] = normalized;
        return out;
    }
    std::unique_ptr<ir::StreamParser> make_stream_parser() const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter() const override;
};

// Parse Responses input item content array (input_text / input_image).
void parse_responses_content(const json &content, std::vector<ContentBlock> &out) {
    if (content.is_string()) {
        ContentBlock b;
        b.kind = ContentKind::Text;
        b.text = content.get<std::string>();
        out.push_back(std::move(b));
        return;
    }
    if (!content.is_array()) return;
    for (const auto &part : content) {
        if (!part.is_object()) continue;
        std::string type = part.value("type", "");
        if (type == "input_text" || type == "output_text") {
            ContentBlock b;
            b.kind = ContentKind::Text;
            b.text = part.value("text", "");
            out.push_back(std::move(b));
        } else if (type == "input_image") {
            ContentBlock b;
            b.kind = ContentKind::Image;
            if (part.contains("image_url")) {
                const json &iu = part["image_url"];
                if (iu.is_string())
                    b.image_url = iu.get<std::string>();
                else if (iu.is_object() && iu.contains("url") && iu["url"].is_string())
                    b.image_url = iu["url"].get<std::string>();
            }
            if (part.contains("detail") && part["detail"].is_string())
                b.extra["detail"] = part["detail"];
            std::string media, b64;
            if (fmt::parse_data_uri(b.image_url, media, b64)) {
                b.image_data_b64 = b64;
                b.media_type = media;
                b.image_url.clear();
            }
            out.push_back(std::move(b));
        }
    }
}

json serialize_responses_content(const std::vector<ContentBlock> &blocks,
                                 bool output_style) {
    json arr = json::array();
    for (const auto &b : blocks) {
        if (b.kind == ContentKind::Text) {
            if (b.extra.contains("raw") && b.extra["raw"].is_object()) {
                arr.push_back(b.extra["raw"]);
            } else {
                json p;
                p["type"] = output_style ? "output_text" : "input_text";
                p["text"] = b.text;
                arr.push_back(std::move(p));
            }
        } else if (b.kind == ContentKind::Image) {
            json p;
            p["type"] = "input_image";
            if (!b.image_url.empty()) {
                p["image_url"] = b.image_url;
            } else {
                p["image_url"] = fmt::build_data_uri(b.media_type, b.image_data_b64);
            }
            if (b.extra.contains("detail")) p["detail"] = b.extra["detail"];
            arr.push_back(std::move(p));
        }
    }
    return arr;
}

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
                        b.tool_input = parse_arguments_string(item["arguments"].get<std::string>());
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
        bool consumed = false;
        for (const char *k : kConsumed) {
            if (it.key() == k) { consumed = true; break; }
        }
        if (!consumed) out.extras[it.key()] = it.value();
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

bool ResponsesCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err) const {
    if (!in.is_object()) {
        err = "Responses response must be a JSON object";
        return false;
    }
    if (in.contains("id") && in["id"].is_string())
        out.id = in["id"].get<std::string>();
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("status") && in["status"].is_string())
        out.stop_reason = fmt::responses_status_to_stop(in["status"].get<std::string>());

    if (in.contains("output") && in["output"].is_array()) {
        for (const auto &item : in["output"]) {
            if (!item.is_object()) continue;
            std::string type = item.value("type", "");
            if (type == "message") {
                if (item.contains("content"))
                    parse_responses_content(item["content"], out.content);
            } else if (type == "function_call") {
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                b.tool_call_id = item.value("call_id", "");
                b.tool_name = item.value("name", "");
                if (item.contains("arguments") && item["arguments"].is_string())
                    b.tool_input = parse_arguments_string(item["arguments"].get<std::string>());
                out.content.push_back(std::move(b));
            } else if (type == "reasoning") {
                if (item.contains("summary") && item["summary"].is_array()) {
                    for (const auto &s : item["summary"]) {
                        if (s.is_object() && s.value("type", "") == "summary_text" &&
                            s.contains("text") && s["text"].is_string()) {
                            ContentBlock b;
                            b.kind = ContentKind::Thinking;
                            b.text = s["text"].get<std::string>();
                            out.content.push_back(std::move(b));
                        }
                    }
                }
            }
        }
    }

    if (in.contains("usage") && in["usage"].is_object()) {
        const json &u = in["usage"];
        if (u.contains("input_tokens") && u["input_tokens"].is_number_integer())
            out.usage.prompt_tokens = u["input_tokens"].get<int>();
        if (u.contains("output_tokens") && u["output_tokens"].is_number_integer())
            out.usage.completion_tokens = u["output_tokens"].get<int>();
        if (u.contains("total_tokens") && u["total_tokens"].is_number_integer())
            out.usage.total_tokens = u["total_tokens"].get<int>();
        else
            out.usage.total_tokens = out.usage.prompt_tokens + out.usage.completion_tokens;
        out.usage.cache_read_tokens =
            fmt::read_cache_hit_tokens(u, out.usage.prompt_tokens).value_or(0);
    }
    out.extras["created_at"] = in.contains("created_at") ? in["created_at"] : json(nullptr);
    out.extras["object"] = in.value("object", "response");
    if (in.contains("status")) out.extras["status"] = in["status"];
    return true;
}

json ResponsesCodec::serialize_response(const ir::ChatResponse &in) const {
    json out;
    out["id"] = in.id.empty() ? "resp-proxy" : in.id;
    out["object"] = "response";  // forced — never inherit source format
    out["created_at"] = in.extras.contains("created_at") ? in.extras["created_at"]
                                                         : json(nullptr);
    out["status"] = fmt::stop_reason_to_responses(in.stop_reason);
    out["model"] = in.model;

    json output = json::array();
    json msg_items;  // accumulate message content blocks per message
    std::string text_content;
    json msg_parts = json::array();
    json tool_items = json::array();
    json reasoning_items = json::array();
    for (const auto &b : in.content) {
        switch (b.kind) {
            case ContentKind::Text:
                msg_parts.push_back(fmt::filter_keys(json{{"type", "output_text"}, {"text", b.text}}, {"type", "text"}));
                break;
            case ContentKind::Thinking:
                reasoning_items.push_back(json{{"type", "summary_text"}, {"text", b.text}});
                break;
            case ContentKind::ToolUse: {
                json item;
                item["type"] = "function_call";
                item["id"] = b.tool_call_id.empty() ? "fc_" + std::to_string((uintptr_t)&b)
                                                     : b.tool_call_id;
                item["call_id"] = b.tool_call_id;
                item["name"] = b.tool_name;
                item["arguments"] = b.tool_input.dump();
                item["status"] = "completed";
                output.push_back(std::move(item));
                break;
            }
            case ContentKind::ToolResult:
                break;
        }
    }
    if (!msg_parts.empty()) {
        json item;
        item["id"] = "msg_0";
        item["type"] = "message";
        item["status"] = "completed";
        item["role"] = "assistant";
        item["content"] = std::move(msg_parts);
        output.push_back(std::move(item));
    }
    if (!reasoning_items.empty()) {
        json item;
        item["id"] = "rs_0";
        item["type"] = "reasoning";
        item["summary"] = std::move(reasoning_items);
        output.push_back(std::move(item));
    }
    out["output"] = std::move(output);

    json usage;
    usage["input_tokens"] = in.usage.prompt_tokens;
    usage["output_tokens"] = in.usage.completion_tokens;
    usage["total_tokens"] = in.usage.total_tokens;
    if (in.usage.cache_read_tokens > 0) {
        usage["input_tokens_details"] = json::object();
        usage["input_tokens_details"]["cached_tokens"] = in.usage.cache_read_tokens;
    }
    out["usage"] = std::move(usage);
    return out;
}

// ── Streaming: parser (upstream Responses SSE → IR events) ──────────────

class ResponsesStreamParser : public StreamParser {
public:
    bool feed(const char *data, size_t len, const EmitFn &emit) override {
        bool ok = true;
        auto guard = [&](const StreamEvent &ev) -> bool {
            if (!ok) return false;
            ok = emit(ev);
            return ok;
        };
        sse_.feed(data, len, [&](const std::string &frame) {
            if (!ok) return;
            std::string event_name, payload;
            if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
            if (payload.empty()) return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(j, guard);
        });
        return ok;
    }

    bool finish(const EmitFn &emit) override {
        bool ok = true;
        auto guard = [&](const StreamEvent &ev) -> bool {
            if (!ok) return false;
            ok = emit(ev);
            return ok;
        };
        sse_.finish([&](const std::string &frame) {
            if (!ok) return;
            std::string event_name, payload;
            if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
            if (payload.empty()) return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(j, guard);
        });
        return ok;
    }

private:
    fmt::SseFrameBuffer sse_;

    void handle_frame(const json &j, const EmitFn &emit) {
        std::string type = j.value("type", "");
        if (type == "response.created") {
            StreamEvent ev;
            ev.type = StreamEventType::MessageStart;
            if (j.contains("response") && j["response"].is_object()) {
                const json &r = j["response"];
                if (r.contains("id") && r["id"].is_string())
                    ev.extra["id"] = r["id"];
                if (r.contains("model") && r["model"].is_string())
                    ev.extra["model"] = r["model"];
            }
            if (!emit(ev)) return;
        } else if (type == "response.output_item.added") {
            int index = j.value("output_index", 0);
            if (j.contains("item") && j["item"].is_object()) {
                const json &item = j["item"];
                std::string itype = item.value("type", "");
                if (itype == "function_call") {
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallStart;
                    ev.index = index;
                    ev.text = item.value("call_id", "");
                    ev.arguments = item.value("name", "");
                    if (!emit(ev)) return;
                }
            }
        } else if (type == "response.output_text.delta") {
            int index = j.value("output_index", 0);
            StreamEvent ev;
            ev.type = StreamEventType::ContentTextDelta;
            ev.index = index;
            ev.text = j.value("delta", "");
            if (!emit(ev)) return;
        } else if (type == "response.reasoning_text.delta" ||
                   type == "response.reasoning_summary_text.delta") {
            int index = j.value("output_index", 0);
            StreamEvent ev;
            ev.type = StreamEventType::ContentThinkingDelta;
            ev.index = index;
            ev.text = j.value("delta", "");
            if (!emit(ev)) return;
        } else if (type == "response.function_call_arguments.delta") {
            int index = j.value("output_index", 0);
            StreamEvent ev;
            ev.type = StreamEventType::ToolCallArgumentDelta;
            ev.index = index;
            ev.arguments = j.value("delta", "");
            if (!emit(ev)) return;
        } else if (type == "response.output_item.done") {
            int index = j.value("output_index", 0);
            if (j.contains("item") && j["item"].is_object() &&
                j["item"].value("type", "") == "function_call") {
                const json &item = j["item"];
                StreamEvent ev;
                ev.type = StreamEventType::ToolCallDone;
                ev.index = index;
                if (item.contains("arguments") && item["arguments"].is_string())
                    ev.arguments = item["arguments"].get<std::string>();
                if (!emit(ev)) return;
            }
        } else if (type == "response.completed" || type == "response.incomplete") {
            if (j.contains("response") && j["response"].is_object()) {
                const json &r = j["response"];
                StreamEvent fin;
                fin.type = StreamEventType::MessageFinish;
                fin.stop_reason = fmt::responses_status_to_stop(r.value("status", type));
                if (!emit(fin)) return;
                if (r.contains("usage") && r["usage"].is_object()) {
                    StreamEvent u;
                    u.type = StreamEventType::UsageEvent;
                    u.usage = fmt::parse_usage_json(r["usage"]);
                    if (!emit(u)) return;
                }
            }
        } else if (type == "response.failed" || type == "response.error") {
            StreamEvent ev;
            ev.type = StreamEventType::ErrorEvent;
            if (j.contains("response") && j["response"].is_object() &&
                j["response"].contains("error"))
                ev.extra["error"] = j["response"]["error"];
            else if (j.contains("error"))
                ev.extra["error"] = j["error"];
            else
                ev.extra["error"] = json{{"message", "upstream error"}};
            if (!emit(ev)) return;
        }
    }
};

// ── Streaming: emitter (IR events → Responses SSE frames) ───────────────

class ResponsesStreamEmitter : public StreamEmitter {
public:
    bool emit(const StreamEvent &ev, const Sink &sink) override {
        switch (ev.type) {
            case StreamEventType::MessageStart:
                id_ = ev.extra.value("id", id_);
                model_ = ev.extra.value("model", model_);
                if (!started_) return emit_response_created(sink);
                return true;
            case StreamEventType::ContentTextDelta: {
                if (!started_ && !emit_response_created(sink)) return false;
                int oi = out_index_for(ev.index, kTextStream);
                if (!text_started_.count(oi) &&
                    !emit_text_item_start(sink, oi)) return false;
                {
                    json d;
                    d["type"] = "response.output_text.delta";
                    d["item_id"] = item_id(oi);
                    d["output_index"] = oi;
                    d["content_index"] = 0;
                    d["delta"] = ev.text;
                    if (!sink("data: " + d.dump() + "\n\n")) return false;
                }
                text_[oi] += ev.text;
                return true;
            }
            case StreamEventType::ContentThinkingDelta: {
                if (!started_ && !emit_response_created(sink)) return false;
                int oi = out_index_for(ev.index, kReasoningStream);
                if (!text_started_.count(oi) &&
                    !emit_reasoning_item_start(sink, oi)) return false;
                {
                    json d;
                    d["type"] = "response.reasoning_text.delta";
                    d["item_id"] = item_id(oi);
                    d["output_index"] = oi;
                    d["content_index"] = 0;
                    d["delta"] = ev.text;
                    if (!sink("data: " + d.dump() + "\n\n")) return false;
                }
                reasoning_text_[oi] += ev.text;
                return true;
            }
            case StreamEventType::ToolCallStart:
                if (!started_ && !emit_response_created(sink)) return false;
                {
                    int oi = out_index_for(ev.index, kToolStream);
                    item_kind_[oi] = 1;
                    json item;
                    item["type"] = "function_call";
                    item["id"] = item_id(oi);
                    item["call_id"] = ev.text;
                    item["name"] = ev.arguments;
                    item["arguments"] = "";
                    item["status"] = "in_progress";
                    json d;
                    d["type"] = "response.output_item.added";
                    d["output_index"] = oi;
                    d["item"] = item;
                    tools_[oi] = {ev.text, ev.arguments, ""};
                    if (!sink("data: " + d.dump() + "\n\n")) return false;
                }
                return true;
            case StreamEventType::ToolCallArgumentDelta:
                {
                    int oi = out_index_for(ev.index, kToolStream);
                    json d;
                    d["type"] = "response.function_call_arguments.delta";
                    d["item_id"] = item_id(oi);
                    d["output_index"] = oi;
                    d["delta"] = ev.arguments;
                    tools_[oi].arguments += ev.arguments;
                    return sink("data: " + d.dump() + "\n\n");
                }
            case StreamEventType::ToolCallDone:
                {
                    int oi = out_index_for(ev.index, kToolStream);
                    json item;
                    item["type"] = "function_call";
                    item["id"] = item_id(oi);
                    item["call_id"] = tools_[oi].call_id;
                    item["name"] = tools_[oi].name;
                    item["arguments"] = ev.arguments.empty() ? tools_[oi].arguments : ev.arguments;
                    item["status"] = "completed";
                    json d;
                    d["type"] = "response.output_item.done";
                    d["output_index"] = oi;
                    d["item"] = item;
                    tools_[oi].arguments = item["arguments"];
                    return sink("data: " + d.dump() + "\n\n");
                }
            case StreamEventType::MessageFinish:
                deferred_status_ = fmt::stop_reason_to_responses(ev.stop_reason);
                return true;  // response.completed deferred until usage or finish
            case StreamEventType::UsageEvent:
                last_usage_ = ev.usage;
                if (!deferred_status_.empty() && !completed_emitted_)
                    return emit_completed(sink);
                return true;
            case StreamEventType::ErrorEvent: {
                if (!started_ && !emit_response_created(sink)) return false;
                finished_ = true;
                json err = ev.extra.contains("error") ? ev.extra["error"]
                                                      : json{{"message", ev.extra.value("message", "upstream error")}};
                json r;
                r["id"] = id_;
                r["object"] = "response";
                r["status"] = "failed";
                r["error"] = err;
                return sink("data: " + json{{"type", "response.failed"}, {"response", r}}.dump() + "\n\n");
            }
        }
        return true;
    }

    bool finish(const Sink &sink) override {
        if (!started_) return true;
        if (!finished_ && !completed_emitted_)
            return emit_completed(sink);
        return true;
    }

private:
    struct ToolItem {
        std::string call_id, name, arguments;
    };
    std::string id_ = "resp-proxy", model_ = "unknown";
    bool started_ = false;
    bool finished_ = false;
    bool completed_emitted_ = false;
    std::string deferred_status_ = "completed";
    Usage last_usage_;
    std::map<int, std::string> text_;            // output_index → message text
    std::map<int, std::string> reasoning_text_;  // output_index → reasoning text
    std::map<int, ToolItem> tools_;              // output_index → tool item
    std::map<int, int> item_kind_;               // 0=message, 1=tool, 2=reasoning
    // IR event index → output_index. The OpenAI stream parser reuses choice
    // index 0 for reasoning AND text, and a separate tool index space (0..n)
    // for tool calls, so key by (kind, ir_index) to keep every Responses item
    // distinct.
    std::map<std::pair<int, int>, int> out_index_;
    std::set<int> text_started_;                 // output_index already started
    int next_output_index_ = 0;
    enum { kReasoningStream = 0, kTextStream = 1, kToolStream = 2 };

    std::string item_id(int index) {
        return "item_" + std::to_string(index);
    }

    // Assign a unique Responses output_index per IR item stream (reasoning,
    // message and tool-call are distinct Responses items).
    int out_index_for(int ir_index, int stream_kind) {
        auto key = std::make_pair(stream_kind, ir_index);
        auto it = out_index_.find(key);
        if (it != out_index_.end()) return it->second;
        int oi = next_output_index_++;
        out_index_[key] = oi;
        return oi;
    }

    bool emit_response_created(const Sink &sink) {
        started_ = true;
        json r;
        r["id"] = id_;
        r["object"] = "response";
        r["status"] = "in_progress";
        r["model"] = model_;
        r["output"] = json::array();
        r["usage"] = nullptr;
        return sink("data: " + json{{"type", "response.created"}, {"response", r}}.dump() + "\n\n");
    }

    bool emit_text_item_start(const Sink &sink, int index) {
        text_started_.insert(index);
        item_kind_[index] = 0;
        json item;
        item["id"] = item_id(index);
        item["type"] = "message";
        item["role"] = "assistant";
        item["status"] = "in_progress";
        item["content"] = json::array();
        json d;
        d["type"] = "response.output_item.added";
        d["output_index"] = index;
        d["item"] = item;
        if (!sink("data: " + d.dump() + "\n\n")) return false;
        json part;
        part["type"] = "output_text";
        part["text"] = "";
        part["annotations"] = json::array();
        json p;
        p["type"] = "response.content_part.added";
        p["item_id"] = item_id(index);
        p["output_index"] = index;
        p["content_index"] = 0;
        p["part"] = part;
        return sink("data: " + p.dump() + "\n\n");
    }

    bool emit_reasoning_item_start(const Sink &sink, int index) {
        text_started_.insert(index);
        item_kind_[index] = 2;
        json item;
        item["id"] = item_id(index);
        item["type"] = "reasoning";
        item["summary"] = json::array();
        json d;
        d["type"] = "response.output_item.added";
        d["output_index"] = index;
        d["item"] = item;
        return sink("data: " + d.dump() + "\n\n");
    }

    json build_output() {
        json output = json::array();
        std::set<int> idxs;
        for (auto &kv : out_index_) idxs.insert(kv.second);
        for (int idx : idxs) {
            int kind = item_kind_.count(idx) ? item_kind_[idx] : 0;
            if (kind == 1) {
                json item;
                item["id"] = item_id(idx);
                item["type"] = "function_call";
                item["call_id"] = tools_[idx].call_id;
                item["name"] = tools_[idx].name;
                item["arguments"] = tools_[idx].arguments;
                item["status"] = "completed";
                output.push_back(std::move(item));
            } else if (kind == 2) {
                json item;
                item["id"] = item_id(idx);
                item["type"] = "reasoning";
                json summ;
                summ["type"] = "summary_text";
                summ["text"] = reasoning_text_[idx];
                item["summary"] = json::array({std::move(summ)});
                output.push_back(std::move(item));
            } else {
                json item;
                item["id"] = item_id(idx);
                item["type"] = "message";
                item["status"] = "completed";
                item["role"] = "assistant";
                json part;
                part["type"] = "output_text";
                part["text"] = text_[idx];
                part["annotations"] = json::array();
                item["content"] = json::array({part});
                output.push_back(std::move(item));
            }
        }
        return output;
    }

    bool emit_completed(const Sink &sink) {
        completed_emitted_ = true;
        json r;
        r["id"] = id_;
        r["object"] = "response";
        r["status"] = deferred_status_;
        r["model"] = model_;
        r["output"] = build_output();
        json usage;
        usage["input_tokens"] = last_usage_.prompt_tokens;
        usage["output_tokens"] = last_usage_.completion_tokens;
        usage["total_tokens"] = last_usage_.total_tokens;
        r["usage"] = usage;
        return sink("data: " + json{{"type", "response.completed"}, {"response", r}}.dump() + "\n\n");
    }
};

}  // namespace

std::unique_ptr<ir::StreamParser> ResponsesCodec::make_stream_parser() const {
    return std::make_unique<ResponsesStreamParser>();
}
std::unique_ptr<ir::StreamEmitter> ResponsesCodec::make_stream_emitter() const {
    return std::make_unique<ResponsesStreamEmitter>();
}

std::unique_ptr<FormatCodec> make_responses_codec() {
    return std::make_unique<ResponsesCodec>();
}
