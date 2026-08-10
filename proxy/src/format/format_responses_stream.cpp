#include "format_responses_internal.h"

#include <set>

using namespace ir;

namespace {

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
        std::string type;
        if (j.is_object() && j.contains("type") && j["type"].is_string())
            type = j["type"].get<std::string>();
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
        } else if (type == "response.refusal.delta") {
            // Refusals are semantic, user-visible output.  Treat the delta as
            // text in the format-neutral IR so converted streams preserve it
            // and passthrough TTFT detection does not wait until EOF.
            int index = j.value("output_index", 0);
            StreamEvent ev;
            ev.type = StreamEventType::ContentTextDelta;
            ev.index = index;
            if (j.contains("delta") && j["delta"].is_string())
                ev.text = j["delta"].get<std::string>();
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
        } else if (type == "response.failed" || type == "response.error" ||
                   type == "error") {
            StreamEvent ev;
            ev.type = StreamEventType::ErrorEvent;
            if (j.contains("response") && j["response"].is_object() &&
                j["response"].contains("error"))
                ev.extra["error"] = j["response"]["error"];
            else if (j.contains("error"))
                ev.extra["error"] = j["error"];
            else if (type == "error")
                // Native Responses error events keep code/message/param at
                // the top level rather than under an `error` member.
                ev.extra["error"] = j;
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

std::unique_ptr<ir::StreamParser> make_responses_stream_parser_impl() {
    return std::make_unique<ResponsesStreamParser>();
}

std::unique_ptr<ir::StreamEmitter> make_responses_stream_emitter_impl() {
    return std::make_unique<ResponsesStreamEmitter>();
}
