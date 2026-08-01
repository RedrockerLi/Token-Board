#include "format_anthropic.h"

#include <cstdio>
#include <cstring>
#include <set>

#include "format_common.h"

using namespace ir;

namespace {

const char *kConsumed[] = {
    "model", "system", "messages", "tools", "tool_choice", "max_tokens",
    "temperature", "stop_sequences", "thinking", "stream", "top_p",
    "top_k", "metadata", "service_tier",
};

// Map OpenAI-style reasoning effort to an Anthropic thinking budget.
int effort_to_budget(const std::string &effort) {
    if (effort == "low") return 1024;
    if (effort == "medium") return 2048;
    if (effort == "high") return 4096;
    return 2048;
}

class AnthropicCodec : public FormatCodec {
public:
    AnthropicCodec() : FormatCodec(ir::ApiFormat::Anthropic) {}

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
        out["type"] = "error";
        out["error"] = normalized;
        return out;
    }
    std::unique_ptr<ir::StreamParser> make_stream_parser() const override;
    std::unique_ptr<ir::StreamEmitter> make_stream_emitter() const override;
};

// Parse an Anthropic content block array (also used for system blocks).
void parse_anthropic_blocks(const json &arr, std::vector<ContentBlock> &out) {
    for (const auto &blk : arr) {
        if (!blk.is_object()) continue;
        std::string type = blk.value("type", "");
        if (type == "text") {
            ContentBlock b;
            b.kind = ContentKind::Text;
            if (blk.contains("text") && blk["text"].is_string())
                b.text = blk["text"].get<std::string>();
            if (blk.contains("cache_control"))
                b.extra["cache_control"] = blk["cache_control"];
            out.push_back(std::move(b));
        } else if (type == "image") {
            ContentBlock b;
            b.kind = ContentKind::Image;
            if (blk.contains("source") && blk["source"].is_object()) {
                const json &src = blk["source"];
                std::string st = src.value("type", "");
                if (st == "base64") {
                    b.image_data_b64 = src.value("data", "");
                    b.media_type = src.value("media_type", "");
                } else if (st == "url") {
                    b.image_url = src.value("url", "");
                }
            }
            out.push_back(std::move(b));
        } else if (type == "tool_use") {
            ContentBlock b;
            b.kind = ContentKind::ToolUse;
            b.tool_call_id = blk.value("id", "");
            b.tool_name = blk.value("name", "");
            if (blk.contains("input")) b.tool_input = blk["input"];
            out.push_back(std::move(b));
        } else if (type == "tool_result") {
            ContentBlock b;
            b.kind = ContentKind::ToolResult;
            b.tool_use_id = blk.value("tool_use_id", "");
            const json &c = blk.contains("content") ? blk["content"] : json(nullptr);
            if (c.is_string()) {
                b.text = c.get<std::string>();
            } else if (c.is_array()) {
                std::string txt;
                for (const auto &sub : c)
                    if (sub.is_object() && sub.value("type", "") == "text" &&
                        sub.contains("text") && sub["text"].is_string())
                        txt += sub["text"].get<std::string>();
                b.text = txt;
            }
            out.push_back(std::move(b));
        } else if (type == "thinking") {
            ContentBlock b;
            b.kind = ContentKind::Thinking;
            if (blk.contains("thinking") && blk["thinking"].is_string())
                b.text = blk["thinking"].get<std::string>();
            if (blk.contains("signature") && blk["signature"].is_string())
                b.extra["signature"] = blk["signature"];
            out.push_back(std::move(b));
        } else if (type == "redacted_thinking") {
            ContentBlock b;
            b.kind = ContentKind::Thinking;
            b.extra["redacted"] = true;
            if (blk.contains("data") && blk["data"].is_string())
                b.extra["data"] = blk["data"];
            out.push_back(std::move(b));
        }
    }
}

bool AnthropicCodec::parse_request(const json &in, ir::ChatRequest &out,
                                   std::string &err) const {
    if (!in.is_object()) {
        err = "Anthropic request must be a JSON object";
        return false;
    }
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("stream") && in["stream"].is_boolean())
        out.stream = in["stream"].get<bool>();

    if (in.contains("system")) {
        const json &s = in["system"];
        if (s.is_string()) {
            ContentBlock b;
            b.kind = ContentKind::Text;
            b.text = s.get<std::string>();
            out.system.push_back(std::move(b));
        } else if (s.is_array()) {
            parse_anthropic_blocks(s, out.system);
        }
    }

    if (in.contains("messages") && in["messages"].is_array()) {
        for (const auto &m : in["messages"]) {
            if (!m.is_object()) continue;
            Message msg;
            msg.role = m.value("role", "");
            const json &c = m.contains("content") ? m["content"] : json(nullptr);
            if (c.is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Text;
                b.text = c.get<std::string>();
                msg.content.push_back(std::move(b));
            } else if (c.is_array()) {
                parse_anthropic_blocks(c, msg.content);
            }
            out.messages.push_back(std::move(msg));
        }
    }

    if (in.contains("tools") && in["tools"].is_array()) {
        for (const auto &t : in["tools"]) {
            if (!t.is_object()) continue;
            Tool tool;
            tool.name = t.value("name", "");
            tool.description = t.value("description", "");
            if (t.contains("input_schema") && t["input_schema"].is_object())
                tool.input_schema = t["input_schema"];
            if (t.contains("cache_control"))
                tool.extra["cache_control"] = t["cache_control"];
            out.tools.push_back(std::move(tool));
        }
    }
    if (in.contains("tool_choice"))
        out.tool_choice = in["tool_choice"];

    if (in.contains("thinking") && in["thinking"].is_object()) {
        const json &th = in["thinking"];
        std::string tt = th.value("type", "");
        if (tt == "enabled") {
            out.reasoning.enabled = true;
            if (th.contains("budget_tokens") && th["budget_tokens"].is_number_integer())
                out.reasoning.budget_tokens = th["budget_tokens"].get<int>();
        }
        out.reasoning.extra = th;
    }

    if (in.contains("max_tokens") && in["max_tokens"].is_number_integer())
        out.max_tokens = in["max_tokens"].get<int>();
    if (in.contains("temperature") && in["temperature"].is_number())
        out.temperature = in["temperature"].get<double>();

    if (in.contains("stop_sequences") && in["stop_sequences"].is_array()) {
        for (const auto &s : in["stop_sequences"])
            if (s.is_string()) out.stop_sequences.push_back(s.get<std::string>());
    }

    for (const auto &it : in.items()) {
        bool consumed = false;
        for (const char *k : kConsumed) {
            if (it.key() == k) { consumed = true; break; }
        }
        if (!consumed) out.extras[it.key()] = it.value();
    }
    return true;
}

json serialize_anthropic_blocks(const std::vector<ContentBlock> &blocks) {
    json arr = json::array();
    for (const auto &b : blocks) {
        switch (b.kind) {
            case ContentKind::Text: {
                json j;
                j["type"] = "text";
                j["text"] = b.text;
                if (b.extra.contains("cache_control"))
                    j["cache_control"] = b.extra["cache_control"];
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::Image: {
                json j;
                j["type"] = "image";
                if (!b.image_data_b64.empty()) {
                    j["source"]["type"] = "base64";
                    j["source"]["media_type"] = b.media_type;
                    j["source"]["data"] = b.image_data_b64;
                } else {
                    j["source"]["type"] = "url";
                    j["source"]["url"] = b.image_url;
                }
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::ToolUse: {
                json j;
                j["type"] = "tool_use";
                j["id"] = b.tool_call_id;
                j["name"] = b.tool_name;
                j["input"] = b.tool_input.is_object() ? b.tool_input : json::object();
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::ToolResult: {
                json j;
                j["type"] = "tool_result";
                j["tool_use_id"] = b.tool_use_id;
                j["content"] = b.text;
                arr.push_back(std::move(j));
                break;
            }
            case ContentKind::Thinking: {
                json j;
                if (b.extra.contains("redacted") && b.extra["redacted"].get<bool>()) {
                    j["type"] = "redacted_thinking";
                    if (b.extra.contains("data")) j["data"] = b.extra["data"];
                } else {
                    j["type"] = "thinking";
                    j["thinking"] = b.text;
                    if (b.extra.contains("signature"))
                        j["signature"] = b.extra["signature"];
                }
                arr.push_back(std::move(j));
                break;
            }
        }
    }
    return arr;
}

json AnthropicCodec::serialize_request(const ir::ChatRequest &in) const {
    json body = fmt::filter_keys(in.extras, {"top_p", "top_k", "metadata",
                                             "service_tier"});

    body["model"] = in.model;
    int max_tokens = in.max_tokens.value_or(0);
    if (max_tokens <= 0) {
        max_tokens = 4096;
        fprintf(stderr, "[Anthropic] max_tokens missing, defaulting to %d\n",
                max_tokens);
    }
    body["max_tokens"] = max_tokens;
    body["stream"] = in.stream;

    if (in.reasoning.enabled) {
        json th;
        th["type"] = "enabled";
        if (in.reasoning.budget_tokens.has_value())
            th["budget_tokens"] = *in.reasoning.budget_tokens;
        else if (!in.reasoning.effort.empty())
            th["budget_tokens"] = effort_to_budget(in.reasoning.effort);
        body["thinking"] = std::move(th);
    } else if (in.reasoning.extra.contains("type")) {
        body["thinking"] = in.reasoning.extra;
    }

    if (in.temperature.has_value()) body["temperature"] = *in.temperature;
    if (!in.stop_sequences.empty()) body["stop_sequences"] = in.stop_sequences;
    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = fmt::normalize_tool_choice_to_anthropic(in.tool_choice);

    if (!in.tools.empty()) {
        json arr = json::array();
        for (const auto &t : in.tools) {
            json j;
            j["name"] = t.name;
            j["description"] = t.description;
            j["input_schema"] = t.input_schema.is_object() ? t.input_schema
                                                           : json::object();
            if (t.extra.contains("cache_control"))
                j["cache_control"] = t.extra["cache_control"];
            arr.push_back(std::move(j));
        }
        body["tools"] = std::move(arr);
    }

    // System: top-level string or block array.
    if (!in.system.empty()) {
        bool all_text = true;
        std::string joined;
        for (const auto &b : in.system) {
            if (b.kind != ContentKind::Text) { all_text = false; break; }
            joined += b.text;
        }
        if (all_text)
            body["system"] = joined;
        else
            body["system"] = serialize_anthropic_blocks(in.system);
    }

    json msgs = json::array();
    for (const auto &m : in.messages) {
        json jm;
        jm["role"] = m.role;
        jm["content"] = serialize_anthropic_blocks(m.content);
        msgs.push_back(std::move(jm));
    }
    body["messages"] = std::move(msgs);
    return body;
}

bool AnthropicCodec::parse_response(const json &in, ir::ChatResponse &out,
                                    std::string &err) const {
    if (!in.is_object()) {
        err = "Anthropic response must be a JSON object";
        return false;
    }
    if (in.contains("id") && in["id"].is_string())
        out.id = in["id"].get<std::string>();
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("content") && in["content"].is_array())
        parse_anthropic_blocks(in["content"], out.content);
    if (in.contains("stop_reason") && in["stop_reason"].is_string()) {
        std::string sr = in["stop_reason"].get<std::string>();
        out.stop_reason = fmt::anthropic_stop_reason_to_stop(sr);
        if (sr == "stop_sequence" && in.contains("stop_sequence") &&
            in["stop_sequence"].is_string())
            out.stop_sequence = in["stop_sequence"].get<std::string>();
    }
    if (in.contains("usage") && in["usage"].is_object()) {
        const json &u = in["usage"];
        if (u.contains("input_tokens") && u["input_tokens"].is_number_integer())
            out.usage.prompt_tokens = u["input_tokens"].get<int>();
        if (u.contains("output_tokens") && u["output_tokens"].is_number_integer())
            out.usage.completion_tokens = u["output_tokens"].get<int>();
        if (u.contains("cache_read_input_tokens") && u["cache_read_input_tokens"].is_number_integer())
            out.usage.cache_read_tokens = u["cache_read_input_tokens"].get<int>();
        if (u.contains("cache_creation_input_tokens") && u["cache_creation_input_tokens"].is_number_integer())
            out.usage.cache_creation_tokens = u["cache_creation_input_tokens"].get<int>();
        out.usage.total_tokens = out.usage.prompt_tokens + out.usage.completion_tokens;
    }
    if (in.contains("type")) out.extras["type"] = in["type"];
    if (in.contains("role")) out.extras["role"] = in["role"];
    return true;
}

json AnthropicCodec::serialize_response(const ir::ChatResponse &in) const {
    json out;
    out["id"] = in.id.empty() ? "msg-proxy" : in.id;
    out["type"] = "message";  // forced — never inherit source format
    out["role"] = "assistant";
    out["model"] = in.model;
    out["content"] = serialize_anthropic_blocks(in.content);
    out["stop_reason"] = fmt::stop_reason_to_anthropic(in.stop_reason);
    if (in.stop_sequence.has_value())
        out["stop_sequence"] = *in.stop_sequence;
    else
        out["stop_sequence"] = nullptr;
    json usage;
    usage["input_tokens"] = in.usage.prompt_tokens;
    usage["output_tokens"] = in.usage.completion_tokens;
    usage["cache_read_input_tokens"] = in.usage.cache_read_tokens;
    usage["cache_creation_input_tokens"] = in.usage.cache_creation_tokens;
    out["usage"] = std::move(usage);
    return out;
}

// ── Streaming: parser (upstream Anthropic SSE → IR events) ──────────────

class AnthropicStreamParser : public StreamParser {
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
            handle_frame(event_name, j, guard);
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
            handle_frame(event_name, j, guard);
        });
        return ok;
    }

private:
    fmt::SseFrameBuffer sse_;
    std::map<int, std::string> tool_args_;
    std::map<int, bool> open_tool_;

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
                if (m.contains("usage") && m["usage"].is_object())
                    ev.usage = fmt::parse_usage_json(m["usage"]);
            }
            if (!emit(ev)) return;
        } else if (type == "content_block_start") {
            int index = j.value("index", 0);
            if (j.contains("content_block") && j["content_block"].is_object()) {
                const json &cb = j["content_block"];
                std::string ctype = cb.value("type", "");
                if (ctype == "tool_use") {
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
                    tool_args_[index] += frag;
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
                StreamEvent ev;
                ev.type = StreamEventType::UsageEvent;
                ev.usage = fmt::parse_usage_json(j["usage"]);
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
                if (!started_ && !emit_message_start(sink)) return false;
                finished_ = true;
                json err = ev.extra.contains("error") ? ev.extra["error"]
                                                      : json{{"message", ev.extra.value("message", "upstream error")}};
                return sink(frame("error", json{{"type", "error"}, {"error", err}}));
            }
        }
        return true;
    }

    bool finish(const Sink &sink) override {
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
        msg["usage"] = {{"input_tokens", 0}, {"output_tokens", 0}};
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
};

}  // namespace

std::unique_ptr<ir::StreamParser> AnthropicCodec::make_stream_parser() const {
    return std::make_unique<AnthropicStreamParser>();
}
std::unique_ptr<ir::StreamEmitter> AnthropicCodec::make_stream_emitter() const {
    return std::make_unique<AnthropicStreamEmitter>();
}

std::unique_ptr<FormatCodec> make_anthropic_codec() {
    return std::make_unique<AnthropicCodec>();
}
