#include "format_openai_internal.h"

using namespace ir;

namespace {

class OpenAIStreamParser : public StreamParser {
public:
    explicit OpenAIStreamParser(const ConversionContext *context)
        : context_(context) {}

    bool feed(const char *data, size_t len, const EmitFn &emit) override {
        bool ok = true;
        auto guard = [&](const StreamEvent &ev) -> bool {
            if (!ok) return false;
            ok = emit(ev);
            return ok;
        };
        const bool buffered = sse_.feed(data, len, [&](const std::string &frame) {
            if (!ok) return;
            std::string event_name, payload;
            if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
            if (payload == "[DONE]") return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(j, guard);
        });
        if (!buffered) emit_failure(guard, "SSE frame exceeds 4 MiB limit");
        return ok && !failed_;
    }

    bool finish(const EmitFn &emit) override {
        bool ok = true;
        auto guard = [&](const StreamEvent &ev) -> bool {
            if (!ok) return false;
            ok = emit(ev);
            return ok;
        };
        const bool buffered = sse_.finish([&](const std::string &frame) {
            if (!ok) return;
            std::string event_name, payload;
            if (!fmt::parse_sse_frame(frame, &event_name, &payload)) return;
            if (payload == "[DONE]") return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(j, guard);
        });
        if (!buffered) emit_failure(guard, "SSE frame exceeds 4 MiB limit");
        if (ok && !failed_) flush_tool_done(guard);
        return ok && !failed_;
    }

private:
    struct ActiveTool {
        std::string id, name, arguments;
        bool start_emitted = false;
    };
    fmt::SseFrameBuffer sse_;
    const ConversionContext *context_ = nullptr;
    std::map<int, ActiveTool> tool_calls_;
    std::string id_, model_;
    bool started_ = false;
    bool failed_ = false;

    const ToolMapping *mapping_for(const std::string &name) const {
        if (!context_) return nullptr;
        for (const auto &mapping : context_->tools.mappings)
            if (mapping.flat_name == name) return &mapping;
        return nullptr;
    }

    void emit_failure(const EmitFn &emit, const std::string &message) {
        if (failed_) return;
        failed_ = true;
        StreamEvent ev;
        ev.type = StreamEventType::ErrorEvent;
        ev.extra["error"] = json{{"message", message},
                                  {"type", "stream_limit_error"},
                                  {"code", 502}};
        emit(ev);
    }

    void flush_tool_done(const EmitFn &emit) {
        for (auto &kv : tool_calls_) {
            StreamEvent ev;
            ev.type = StreamEventType::ToolCallDone;
            ev.index = kv.first;
            ev.arguments = kv.second.arguments;
            if (!emit(ev)) return;
        }
        tool_calls_.clear();
    }

    // Extract text from a delta content field that may be a plain string or an
    // array of parts ({"type":"text","text":...}) sent by some OpenAI-compatible
    // gateways. Returns empty if nothing textual.
    static std::string delta_text(const json &v) {
        if (v.is_string()) return v.get<std::string>();
        if (v.is_array()) {
            std::string out;
            for (const auto &p : v) {
                if (p.is_string()) {
                    out += p.get<std::string>();
                } else if (p.is_object() && p.value("type", "") == "text" &&
                           p.contains("text") && p["text"].is_string()) {
                    out += p["text"].get<std::string>();
                }
            }
            return out;
        }
        return std::string();
    }

    void handle_frame(const json &j, const EmitFn &emit) {
        // OpenAI-compatible providers commonly return an HTTP 200 SSE stream
        // whose terminal frame is a top-level {"error": ...} object.  Surface
        // it as an IR error so the routing layer can retry an uncommitted
        // request instead of treating the transport status as success.
        if (j.is_object() && j.contains("error") && !j["error"].is_null()) {
            StreamEvent ev;
            ev.type = StreamEventType::ErrorEvent;
            ev.extra["error"] = j["error"];
            emit(ev);
            return;
        }

        if (j.contains("id") && j["id"].is_string())
            id_ = j["id"].get<std::string>();
        if (j.contains("model") && j["model"].is_string())
            model_ = j["model"].get<std::string>();

        if (!started_ && (!id_.empty() || !model_.empty())) {
            started_ = true;
            StreamEvent ev;
            ev.type = StreamEventType::MessageStart;
            if (!id_.empty()) ev.extra["id"] = id_;
            if (!model_.empty()) ev.extra["model"] = model_;
            if (!emit(ev)) return;
        }

        if (j.contains("usage") && j["usage"].is_object()) {
            StreamEvent ev;
            ev.type = StreamEventType::UsageEvent;
            ev.usage = fmt::parse_usage_json(j["usage"]);
            if (!emit(ev)) return;
        }

        if (!j.contains("choices") || !j["choices"].is_array()) return;
        for (const auto &ch : j["choices"]) {
            if (!ch.is_object()) continue;
            int index = ch.value("index", 0);

            if (ch.contains("finish_reason") && !ch["finish_reason"].is_null()) {
                flush_tool_done(emit);
                StreamEvent ev;
                ev.type = StreamEventType::MessageFinish;
                ev.index = index;
                ev.stop_reason = fmt::openai_finish_reason_to_stop(
                    ch["finish_reason"].get<std::string>());
                if (!emit(ev)) return;
            }

            if (!ch.contains("delta") || !ch["delta"].is_object()) continue;
            const json &d = ch["delta"];
            if (d.contains("content") && d["content"].is_string()) {
                StreamEvent ev;
                ev.type = StreamEventType::ContentTextDelta;
                ev.index = index;
                ev.text = d["content"].get<std::string>();
                if (!emit(ev)) return;
            } else if (d.contains("content") && d["content"].is_array()) {
                std::string txt = delta_text(d["content"]);
                if (!txt.empty()) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ContentTextDelta;
                    ev.index = index;
                    ev.text = std::move(txt);
                    if (!emit(ev)) return;
                }
            }
            // Refusal deltas are user-visible semantic output too.  Mapping
            // them to text keeps cross-format streams meaningful and, for
            // passthrough metrics parsing, lets the semantic-TTFT watchdog see
            // that the upstream has begun responding.
            if (d.contains("refusal") && d["refusal"].is_string() &&
                !d["refusal"].get_ref<const std::string &>().empty()) {
                StreamEvent ev;
                ev.type = StreamEventType::ContentTextDelta;
                ev.index = index;
                ev.text = d["refusal"].get<std::string>();
                if (!emit(ev)) return;
            }
            if (d.contains("reasoning_content") && d["reasoning_content"].is_string()) {
                StreamEvent ev;
                ev.type = StreamEventType::ContentThinkingDelta;
                ev.index = index;
                ev.text = d["reasoning_content"].get<std::string>();
                if (!emit(ev)) return;
            } else if (d.contains("reasoning_content") && d["reasoning_content"].is_array()) {
                std::string txt = delta_text(d["reasoning_content"]);
                if (!txt.empty()) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ContentThinkingDelta;
                    ev.index = index;
                    ev.text = std::move(txt);
                    if (!emit(ev)) return;
                }
            }
            if (d.contains("tool_calls") && d["tool_calls"].is_array()) {
                for (const auto &tc : d["tool_calls"]) {
                    if (!tc.is_object()) continue;
                    int tindex = tc.value("index", 0);
                    auto found = tool_calls_.find(tindex);
                    if (found == tool_calls_.end()) {
                        if (tool_calls_.size() >= fmt::kMaxStreamItems) {
                            emit_failure(emit, "too many streamed tool calls");
                            return;
                        }
                        found = tool_calls_.emplace(tindex, ActiveTool{}).first;
                    }
                    auto &at = found->second;
                    // 1. identity
                    if (tc.contains("id") && tc["id"].is_string())
                        at.id = tc["id"].get<std::string>();
                    if (tc.contains("function") && tc["function"].is_object() &&
                        tc["function"].contains("name") && tc["function"]["name"].is_string())
                        at.name = tc["function"]["name"].get<std::string>();
                    // 2. ToolCallStart first — Anthropic requires
                    //    content_block_start before any delta for that block.
                    if (!at.id.empty() && !at.start_emitted) {
                        at.start_emitted = true;
                        StreamEvent ev;
                        ev.type = StreamEventType::ToolCallStart;
                        ev.index = tindex;
                        ev.text = at.id;
                        ev.arguments = at.name;
                        if (const auto *mapping = mapping_for(at.name)) {
                            ev.namespace_name = mapping->namespace_name;
                            ev.item.name = mapping->original_name;
                            ev.item.namespace_name = mapping->namespace_name;
                            ev.item.item_kind = mapping->kind == ToolKind::Custom
                                ? ItemKind::CustomToolCall
                                : mapping->kind == ToolKind::ToolSearch
                                    ? ItemKind::ToolSearchCall
                                    : ItemKind::FunctionCall;
                            ev.extra["tool_kind"] = mapping->kind == ToolKind::Custom
                                ? "custom_tool_call"
                                : mapping->kind == ToolKind::ToolSearch
                                    ? "tool_search_call" : "function_call";
                        }
                        if (!emit(ev)) return;
                        // Flush fragments buffered before the id arrived.
                        if (!at.arguments.empty()) {
                            StreamEvent de;
                            de.type = StreamEventType::ToolCallArgumentDelta;
                            de.index = tindex;
                            de.arguments = at.arguments;
                            if (!emit(de)) return;
                        }
                    }
                    // 3. argument deltas (non-empty only, and only after start)
                    if (tc.contains("function") && tc["function"].is_object() &&
                        tc["function"].contains("arguments")) {
                        const json &af = tc["function"]["arguments"];
                        std::string args = af.is_string() ? af.get<std::string>()
                                                          : af.dump();
                        if (args.size() > fmt::kMaxToolArgumentsBytes ||
                            at.arguments.size() >
                                fmt::kMaxToolArgumentsBytes - args.size()) {
                            emit_failure(emit,
                                         "streamed tool arguments exceed 8 MiB limit");
                            return;
                        }
                        at.arguments += args;  // full accumulation for ToolCallDone
                        if (at.start_emitted && !args.empty()) {
                            StreamEvent ev;
                            ev.type = StreamEventType::ToolCallArgumentDelta;
                            ev.index = tindex;
                            ev.arguments = args;
                            if (!emit(ev)) return;
                        }
                    }
                }
            }
        }
    }
};

// ── Streaming: emitter (IR events → OpenAI SSE chunks) ──────────────────

class OpenAIStreamEmitter : public StreamEmitter {
public:
    bool emit(const StreamEvent &ev, const Sink &sink) override {
        switch (ev.type) {
            case StreamEventType::MessageStart:
                id_ = ev.extra.value("id", id_);
                model_ = ev.extra.value("model", model_);
                last_usage_ = ev.usage;
                return true;  // role chunk emitted lazily on first content
            case StreamEventType::ContentTextDelta:
                if (!started_ && !emit_role_chunk(sink)) return false;
                return sink("data: " + chunk({{"content", ev.text}}, ev.index).dump() + "\n\n");
            case StreamEventType::ContentThinkingDelta:
                if (!started_ && !emit_role_chunk(sink)) return false;
                return sink("data: " + chunk({{"reasoning_content", ev.text}}, ev.index).dump() + "\n\n");
            case StreamEventType::ToolCallStart: {
                if (!started_ && !emit_role_chunk(sink)) return false;
                int tool_index = 0;
                if (!tool_index_for(ev.index, tool_index))
                    return emit_limit_error(sink, "too many streamed tool calls");
                json tc;
                tc["index"] = tool_index;
                tc["id"] = ev.text;
                tc["type"] = "function";
                tc["function"] = json::object();
                tc["function"]["name"] = ev.arguments;
                tc["function"]["arguments"] = "";
                json delta;
                delta["tool_calls"] = json::array({std::move(tc)});
                return sink("data: " + chunk(std::move(delta), 0).dump() + "\n\n");
            }
            case StreamEventType::ToolCallArgumentDelta: {
                if (!started_ && !emit_role_chunk(sink)) return false;
                int tool_index = 0;
                if (!tool_index_for(ev.index, tool_index))
                    return emit_limit_error(sink, "too many streamed tool calls");
                json tc;
                tc["index"] = tool_index;
                tc["function"] = json::object();
                tc["function"]["arguments"] = ev.arguments;
                json delta;
                delta["tool_calls"] = json::array({std::move(tc)});
                return sink("data: " + chunk(std::move(delta), 0).dump() + "\n\n");
            }
            case StreamEventType::ToolCallDone:
                return true;  // no explicit done in OpenAI stream
            case StreamEventType::MessageFinish:
                deferred_finish_ = fmt::stop_reason_to_openai(ev.stop_reason);
                return true;  // emitted after usage (or at finish)
            case StreamEventType::UsageEvent:
                last_usage_ = ev.usage;
                if (started_ && !deferred_finish_.empty() && !finish_emitted_) {
                    if (!emit_finish_chunk(sink)) return false;
                }
                return sink("data: " + usage_chunk(last_usage_).dump() + "\n\n");
            case StreamEventType::ErrorEvent: {
                json err = json::object();
                err["error"] = ev.extra.contains("error") ? ev.extra["error"]
                                                           : json{{"message", ev.extra.value("message", "upstream error")}};
                finished_ = true;
                return sink("data: " + err.dump() + "\n\n");
            }
        }
        return true;
    }

    bool finish(const Sink &sink) override {
        if (failed_) return true;
        if (!deferred_finish_.empty() && !finish_emitted_) {
            if (!emit_finish_chunk(sink)) return false;
        }
        if (!finished_) return sink("data: [DONE]\n\n");
        return true;
    }

private:
    fmt::SseFrameBuffer sse_;
    std::string id_ = "chatcmpl-proxy", model_ = "unknown";
    bool started_ = false;
    std::string deferred_finish_;
    bool finish_emitted_ = false;
    bool finished_ = false;
    bool failed_ = false;
    Usage last_usage_;
    json failure_;
    std::map<int, int> tool_indices_;
    int next_tool_index_ = 0;

    bool tool_index_for(int ir_index, int &index) {
        auto existing = tool_indices_.find(ir_index);
        if (existing != tool_indices_.end()) {
            index = existing->second;
            return true;
        }
        if (tool_indices_.size() >= fmt::kMaxStreamItems) return false;
        auto inserted = tool_indices_.emplace(ir_index, next_tool_index_);
        if (inserted.second) ++next_tool_index_;
        index = inserted.first->second;
        return true;
    }

    bool emit_limit_error(const Sink &sink, const std::string &message) {
        if (failed_) return false;
        failed_ = true;
        finished_ = true;
        failure_ = json{{"message", message},
                        {"type", "stream_limit_error"},
                        {"code", 502}};
        return sink("data: " + json{{"error", failure_}}.dump() + "\n\n");
    }

    json chunk(const json &extra, int index) {
        json body;
        body["id"] = id_;
        body["object"] = "chat.completion.chunk";
        body["created"] = 0;
        body["model"] = model_;
        json choice;
        choice["index"] = index;
        choice["delta"] = json::object();
        choice["finish_reason"] = nullptr;
        for (auto it = extra.begin(); it != extra.end(); ++it)
            choice["delta"][it.key()] = it.value();
        body["choices"] = json::array({std::move(choice)});
        return body;
    }

    bool emit_role_chunk(const Sink &sink) {
        started_ = true;
        return sink("data: " + chunk({{"role", "assistant"}}, 0).dump() + "\n\n");
    }

    json usage_chunk(const Usage &u) {
        json body;
        body["id"] = id_;
        body["object"] = "chat.completion.chunk";
        body["created"] = 0;
        body["model"] = model_;
        body["choices"] = json::array();
        json usage;
        usage["prompt_tokens"] = u.prompt_tokens;
        usage["completion_tokens"] = u.completion_tokens;
        usage["total_tokens"] = u.total_tokens;
        body["usage"] = std::move(usage);
        return body;
    }

    bool emit_finish_chunk(const Sink &sink) {
        finish_emitted_ = true;
        json body;
        body["id"] = id_;
        body["object"] = "chat.completion.chunk";
        body["created"] = 0;
        body["model"] = model_;
        json choice;
        choice["index"] = 0;
        choice["delta"] = json::object();
        choice["finish_reason"] = deferred_finish_;
        body["choices"] = json::array({std::move(choice)});
        return sink("data: " + body.dump() + "\n\n");
    }
};

}  // namespace

std::unique_ptr<ir::StreamParser> make_openai_stream_parser_impl(
    const ConversionContext *context) {
    return std::make_unique<OpenAIStreamParser>(context);
}

std::unique_ptr<ir::StreamEmitter> make_openai_stream_emitter_impl() {
    return std::make_unique<OpenAIStreamEmitter>();
}
