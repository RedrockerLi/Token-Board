#include "format_anthropic_internal.h"

using namespace ir;

namespace {

class AnthropicStreamParser : public StreamParser {
public:
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
            if (payload.empty()) return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(event_name, j, guard);
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
            if (payload.empty()) return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(event_name, j, guard);
        });
        if (!buffered) emit_failure(guard, "SSE frame exceeds 4 MiB limit");
        return ok && !failed_;
    }

private:
    fmt::SseFrameBuffer sse_;
    std::map<int, std::string> tool_args_;
    std::map<int, bool> open_tool_;
    Usage usage_;
    bool failed_ = false;

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

    void merge_usage(const json &u) {
        const Usage delta = fmt::parse_usage_json(u);
        if (u.contains("input_tokens")) usage_.prompt_tokens = delta.prompt_tokens;
        if (u.contains("output_tokens"))
            usage_.completion_tokens = delta.completion_tokens;
        if (u.contains("cache_read_input_tokens"))
            usage_.cache_read_tokens = delta.cache_read_tokens;
        if (u.contains("cache_creation_input_tokens"))
            usage_.cache_creation_tokens = delta.cache_creation_tokens;
        usage_.total_tokens = usage_.prompt_tokens + usage_.completion_tokens;
    }

    void handle_frame(const std::string &event_name, const json &j,
                      const EmitFn &emit) {
        std::string type = j.value("type", event_name);
        if (type == "message_start") {
            StreamEvent ev;
            ev.type = StreamEventType::MessageStart;
            if (j.contains("message") && j["message"].is_object()) {
                const json &m = j["message"];
                if (m.contains("id") && m["id"].is_string())
                    ev.extra["id"] = m["id"];
                if (m.contains("model") && m["model"].is_string())
                    ev.extra["model"] = m["model"];
                if (m.contains("usage") && m["usage"].is_object()) {
                    merge_usage(m["usage"]);
                    ev.usage = usage_;
                }
            }
            if (!emit(ev)) return;
        } else if (type == "content_block_start") {
            int index = j.value("index", 0);
            if (j.contains("content_block") && j["content_block"].is_object()) {
                const json &cb = j["content_block"];
                std::string ctype = cb.value("type", "");
                if (ctype == "tool_use") {
                    if (!open_tool_.count(index) &&
                        open_tool_.size() >= fmt::kMaxStreamItems) {
                        emit_failure(emit, "too many streamed tool calls");
                        return;
                    }
                    open_tool_[index] = true;
                    tool_args_[index] = "";
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallStart;
                    ev.index = index;
                    ev.text = cb.value("id", "");
                    ev.arguments = cb.value("name", "");
                    if (!emit(ev)) return;
                }
            }
        } else if (type == "content_block_delta") {
            int index = j.value("index", 0);
            if (j.contains("delta") && j["delta"].is_object()) {
                const json &d = j["delta"];
                std::string dtype = d.value("type", "");
                if (dtype == "text_delta" && d.contains("text") && d["text"].is_string()) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ContentTextDelta;
                    ev.index = index;
                    ev.text = d["text"].get<std::string>();
                    if (!emit(ev)) return;
                } else if (dtype == "thinking_delta" && d.contains("thinking") && d["thinking"].is_string()) {
                    StreamEvent ev;
                    ev.type = StreamEventType::ContentThinkingDelta;
                    ev.index = index;
                    ev.text = d["thinking"].get<std::string>();
                    if (!emit(ev)) return;
                } else if (dtype == "input_json_delta" && d.contains("partial_json") && d["partial_json"].is_string()) {
                    std::string frag = d["partial_json"].get<std::string>();
                    auto &arguments = tool_args_[index];
                    if (frag.size() > fmt::kMaxToolArgumentsBytes ||
                        arguments.size() >
                            fmt::kMaxToolArgumentsBytes - frag.size()) {
                        emit_failure(emit,
                                     "streamed tool arguments exceed 8 MiB limit");
                        return;
                    }
                    arguments += frag;
                    StreamEvent ev;
                    ev.type = StreamEventType::ToolCallArgumentDelta;
                    ev.index = index;
                    ev.arguments = frag;
                    if (!emit(ev)) return;
                }
            }
        } else if (type == "content_block_stop") {
            int index = j.value("index", 0);
            if (open_tool_.count(index)) {
                open_tool_.erase(index);
                StreamEvent ev;
                ev.type = StreamEventType::ToolCallDone;
                ev.index = index;
                ev.arguments = tool_args_.count(index) ? tool_args_[index] : std::string();
                tool_args_.erase(index);
                if (!emit(ev)) return;
            }
        } else if (type == "message_delta") {
            if (j.contains("delta") && j["delta"].is_object()) {
                const json &d = j["delta"];
                if (d.contains("stop_reason") && d["stop_reason"].is_string()) {
                    StreamEvent ev;
                    ev.type = StreamEventType::MessageFinish;
                    ev.stop_reason = fmt::anthropic_stop_reason_to_stop(
                        d["stop_reason"].get<std::string>());
                    if (!emit(ev)) return;
                }
            }
            if (j.contains("usage") && j["usage"].is_object()) {
                merge_usage(j["usage"]);
                StreamEvent ev;
                ev.type = StreamEventType::UsageEvent;
                ev.usage = usage_;
                if (!emit(ev)) return;
            }
        } else if (type == "error") {
            StreamEvent ev;
            ev.type = StreamEventType::ErrorEvent;
            ev.extra["error"] = j.contains("error") ? j["error"] : json{{"message", "upstream error"}};
            if (!emit(ev)) return;
        }
    }
};

// ── Streaming: emitter (IR events → Anthropic SSE frames) ───────────────

class AnthropicStreamEmitter : public StreamEmitter {
public:
    bool emit(const StreamEvent &ev, const Sink &sink) override {
        switch (ev.type) {
            case StreamEventType::MessageStart:
                id_ = ev.extra.value("id", id_);
                model_ = ev.extra.value("model", model_);
                last_usage_ = ev.usage;
                if (!started_) return emit_message_start(sink);
                return true;
            case StreamEventType::ContentTextDelta:
                if (ev.text.empty()) return true;  // suppress spurious empty fragments
                if (!started_ && !emit_message_start(sink)) return false;
                if (!open_block(sink, ev.index, ContentKind::Text,
                                json{{"type", "text"}, {"text", ""}})) return false;
                return sink(frame("content_block_delta", json{
                    {"type", "content_block_delta"},
                    {"index", open_blocks_[ev.index].anthro_index},
                    {"delta", {{"type", "text_delta"}, {"text", ev.text}}}}));
            case StreamEventType::ContentThinkingDelta:
                if (ev.text.empty()) return true;  // suppress spurious empty fragments
                if (!started_ && !emit_message_start(sink)) return false;
                if (!open_block(sink, ev.index, ContentKind::Thinking,
                                json{{"type", "thinking"}, {"thinking", ""}})) return false;
                return sink(frame("content_block_delta", json{
                    {"type", "content_block_delta"},
                    {"index", open_blocks_[ev.index].anthro_index},
                    {"delta", {{"type", "thinking_delta"}, {"thinking", ev.text}}}}));
            case StreamEventType::ToolCallStart: {
                if (!started_ && !emit_message_start(sink)) return false;
                if (open_blocks_.count(ev.index) &&
                    open_blocks_[ev.index].kind == ContentKind::ToolUse)
                    return true;  // some providers repeat id+name — keep open block
                if (!open_block(sink, ev.index, ContentKind::ToolUse, json{
                        {"type", "tool_use"}, {"id", ev.text},
                        {"name", ev.arguments}, {"input", json::object()}}))
                    return false;
                return true;
            }
            case StreamEventType::ToolCallArgumentDelta: {
                auto it = open_blocks_.find(ev.index);
                if (it == open_blocks_.end() || it->second.kind != ContentKind::ToolUse)
                    return true;  // no open tool block — guard against ordering drift
                return sink(frame("content_block_delta", json{
                    {"type", "content_block_delta"},
                    {"index", it->second.anthro_index},
                    {"delta", {{"type", "input_json_delta"},
                               {"partial_json", ev.arguments}}}}));
            }
            case StreamEventType::ToolCallDone:
                return stop_block(sink, ev.index);
            case StreamEventType::MessageFinish:
                seen_finish_ = true;
                deferred_stop_ = fmt::stop_reason_to_anthropic(ev.stop_reason);
                if (!started_ && !emit_message_start(sink)) return false;
                return true;  // message_delta deferred until usage or finish
            case StreamEventType::UsageEvent:
                last_usage_ = ev.usage;
                if (!started_ && !emit_message_start(sink)) return false;
                // Usage can arrive before finish_reason in the same final chunk;
                // only emit message_delta once the real stop_reason is known.
                if (seen_finish_ && !delta_emitted_) {
                    if (!close_open_blocks(sink)) return false;
                    if (!emit_message_delta(sink)) return false;
                }
                return true;
            case StreamEventType::ErrorEvent: {
                finished_ = true;
                json err = ev.extra.contains("error") ? ev.extra["error"]
                                                      : json{{"message", ev.extra.value("message", "upstream error")}};
                return sink(frame("error", json{{"type", "error"}, {"error", err}}));
            }
        }
        return true;
    }

    bool finish(const Sink &sink) override {
        if (failed_) return true;
        if (!started_) return true;
        if (!finished_) {
            if (!close_open_blocks(sink)) return false;
            if (!delta_emitted_) {
                if (!emit_message_delta(sink)) return false;
            }
            return sink(frame("message_stop", json{{"type", "message_stop"}}));
        }
        return true;
    }

private:
    std::string id_ = "msg-proxy", model_ = "unknown";
    bool started_ = false;
    bool finished_ = false;
    bool delta_emitted_ = false;
    std::string deferred_stop_ = "end_turn";
    Usage last_usage_;
    struct OpenBlock {
        int anthro_index = 0;
        ContentKind kind = ContentKind::Text;
    };
    std::map<int, OpenBlock> open_blocks_;  // source index → open Anthropic block
    int next_index_ = 0;                    // monotonically increasing Anthropic block index
    bool seen_finish_ = false;              // a MessageFinish has been seen
    bool failed_ = false;
    json failure_;

    std::string frame(const std::string &event, const json &data) {
        return "event: " + event + "\ndata: " + data.dump() + "\n\n";
    }

    bool emit_message_start(const Sink &sink) {
        started_ = true;
        json msg;
        msg["id"] = id_;
        msg["type"] = "message";
        msg["role"] = "assistant";
        msg["model"] = model_;
        msg["content"] = json::array();
        int input = last_usage_.prompt_tokens;
        msg["usage"] = {{"input_tokens", input < 0 ? 0 : input},
                        {"output_tokens", last_usage_.completion_tokens},
                        {"cache_read_input_tokens",
                         last_usage_.cache_read_tokens},
                        {"cache_creation_input_tokens",
                         last_usage_.cache_creation_tokens}};
        return sink(frame("message_start", json{{"type", "message_start"}, {"message", msg}}));
    }

    // Open an Anthropic content block for source index `src_index`, assigning a
    // fresh sequential block index (decoupled from the source event index so
    // text/thinking/tool blocks sharing a source index don't collide).  If a
    // block of a *different* kind is already open at that source index, close it
    // first; a same-kind open block is kept.
    bool open_block(const Sink &sink, int src_index, ContentKind kind,
                    const json &content_block) {
        auto it = open_blocks_.find(src_index);
        if (it != open_blocks_.end()) {
            if (it->second.kind == kind) return true;
            if (!stop_block(sink, src_index)) return false;
        }
        if (open_blocks_.size() >= fmt::kMaxStreamItems)
            return emit_limit_error(sink, "too many streamed content blocks");
        int idx = next_index_++;
        open_blocks_[src_index] = {idx, kind};
        return sink(frame("content_block_start", json{
            {"type", "content_block_start"}, {"index", idx},
            {"content_block", content_block}}));
    }

    // Close the block open at source index `src_index`, if any.  No-op when the
    // index has no open block (e.g. a tool call that never emitted a start).
    bool stop_block(const Sink &sink, int src_index) {
        auto it = open_blocks_.find(src_index);
        if (it == open_blocks_.end()) return true;
        bool ok = sink(frame("content_block_stop", json{
            {"type", "content_block_stop"}, {"index", it->second.anthro_index}}));
        open_blocks_.erase(it);
        return ok;
    }

    bool close_open_blocks(const Sink &sink) {
        for (auto &kv : open_blocks_) {
            if (!sink(frame("content_block_stop", json{
                    {"type", "content_block_stop"}, {"index", kv.second.anthro_index}})))
                return false;
        }
        open_blocks_.clear();
        return true;
    }

    bool emit_message_delta(const Sink &sink) {
        delta_emitted_ = true;
        json usage;
        // Anthropic's input_tokens excludes cache hits (the three buckets are
        // mutually exclusive) — mirror cc-switch's build_anthropic_usage_json:
        // input = prompt - cache_read - cache_creation.
        int input = last_usage_.prompt_tokens - last_usage_.cache_read_tokens
                    - last_usage_.cache_creation_tokens;
        usage["input_tokens"] = input < 0 ? 0 : input;
        usage["output_tokens"] = last_usage_.completion_tokens;
        usage["cache_read_input_tokens"] = last_usage_.cache_read_tokens;
        usage["cache_creation_input_tokens"] = last_usage_.cache_creation_tokens;
        json d;
        d["stop_reason"] = deferred_stop_;
        d["stop_sequence"] = nullptr;
        return sink(frame("message_delta", json{
            {"type", "message_delta"}, {"delta", d}, {"usage", usage}}));
    }

    bool emit_limit_error(const Sink &sink, const std::string &message) {
        if (failed_) return false;
        failed_ = true;
        finished_ = true;
        failure_ = json{{"message", message},
                        {"type", "stream_limit_error"},
                        {"code", 502}};
        sink(frame("error", json{{"type", "error"},
                                  {"error", failure_}}));
        return false;
    }
};

}  // namespace

std::unique_ptr<ir::StreamParser> make_anthropic_stream_parser_impl() {
    return std::make_unique<AnthropicStreamParser>();
}

std::unique_ptr<ir::StreamEmitter> make_anthropic_stream_emitter_impl() {
    return std::make_unique<AnthropicStreamEmitter>();
}
