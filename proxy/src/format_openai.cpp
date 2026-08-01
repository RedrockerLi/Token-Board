#include "format_openai.h"

#include <cstring>

#include "format_common.h"

using namespace ir;

namespace {

// Keys consumed (mapped to IR fields) — the rest land in IR extras.
const char *kConsumed[] = {
    "model", "messages", "system", "tools", "tool_choice", "stream",
    "stream_options", "reasoning_effort", "reasoning", "max_tokens",
    "max_completion_tokens", "temperature", "stop",
};

// Extra keys this format can forward through (not regenerated).

class OpenAICodec : public FormatCodec {
public:
    OpenAICodec() : FormatCodec(ir::ApiFormat::OpenAI) {}

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

// ── Request ─────────────────────────────────────────────────────────────

bool OpenAICodec::parse_request(const json &in, ir::ChatRequest &out,
                                std::string &err) const {
    if (!in.is_object()) {
        err = "OpenAI request must be a JSON object";
        return false;
    }
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();
    if (in.contains("stream") && in["stream"].is_boolean())
        out.stream = in["stream"].get<bool>();

    if (in.contains("messages") && in["messages"].is_array()) {
        for (const auto &m : in["messages"]) {
            if (!m.is_object()) continue;
            std::string role = m.value("role", "");
            Message msg;
            msg.role = role;

            // content (string or part array)
            if (m.contains("content") && m["content"].is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Text;
                b.text = m["content"].get<std::string>();
                msg.content.push_back(std::move(b));
            } else if (m.contains("content") && m["content"].is_array()) {
                for (const auto &part : m["content"]) {
                    if (!part.is_object()) {
                        ContentBlock b;
                        b.kind = ContentKind::Text;
                        if (part.is_string())
                            b.text = part.get<std::string>();
                        msg.content.push_back(std::move(b));
                        continue;
                    }
                    std::string type = part.value("type", "");
                    if (type == "text" && part.contains("text") &&
                        part["text"].is_string()) {
                        ContentBlock b;
                        b.kind = ContentKind::Text;
                        b.text = part["text"].get<std::string>();
                        msg.content.push_back(std::move(b));
                    } else if (type == "image_url") {
                        msg.content.push_back(fmt::openai_image_part_to_block(part));
                    } else {
                        // Unknown part type — keep raw so it survives round-trip.
                        ContentBlock b;
                        b.kind = ContentKind::Text;
                        b.extra["raw"] = part;
                        msg.content.push_back(std::move(b));
                    }
                }
            }

            // tool_calls
            if (m.contains("tool_calls") && m["tool_calls"].is_array()) {
                for (const auto &tc : m["tool_calls"]) {
                    if (!tc.is_object()) continue;
                    ContentBlock b;
                    b.kind = ContentKind::ToolUse;
                    if (tc.contains("id") && tc["id"].is_string())
                        b.tool_call_id = tc["id"].get<std::string>();
                    if (tc.contains("type") && tc["type"].is_string())
                        b.extra["type"] = tc["type"];
                    if (tc.contains("function") && tc["function"].is_object()) {
                        const json &fn = tc["function"];
                        if (fn.contains("name") && fn["name"].is_string())
                            b.tool_name = fn["name"].get<std::string>();
                        if (fn.contains("arguments")) {
                            std::string args =
                                fn["arguments"].is_string()
                                    ? fn["arguments"].get<std::string>()
                                    : fn["arguments"].dump();
                            try {
                                b.tool_input = json::parse(args);
                            } catch (...) {
                                b.tool_input = json::object();
                            }
                        }
                    }
                    msg.content.push_back(std::move(b));
                }
            }
            // legacy function_call
            if (m.contains("function_call") && m["function_call"].is_object()) {
                ContentBlock b;
                b.kind = ContentKind::ToolUse;
                const json &fc = m["function_call"];
                if (fc.contains("name") && fc["name"].is_string())
                    b.tool_name = fc["name"].get<std::string>();
                if (fc.contains("arguments")) {
                    std::string args =
                        fc["arguments"].is_string()
                            ? fc["arguments"].get<std::string>()
                            : fc["arguments"].dump();
                    try {
                        b.tool_input = json::parse(args);
                    } catch (...) {
                        b.tool_input = json::object();
                    }
                }
                msg.content.push_back(std::move(b));
            }

            // tool result message (role "tool")
            if (role == "tool") {
                ContentBlock b;
                b.kind = ContentKind::ToolResult;
                if (m.contains("tool_call_id") && m["tool_call_id"].is_string())
                    b.tool_use_id = m["tool_call_id"].get<std::string>();
                if (!msg.content.empty()) {
                    b.text = msg.content[0].text;
                } else if (m.contains("content") && m["content"].is_string()) {
                    b.text = m["content"].get<std::string>();
                }
                msg.content.clear();
                msg.content.push_back(std::move(b));
            }

            // reasoning content on assistant messages
            if (m.contains("reasoning_content") &&
                m["reasoning_content"].is_string() &&
                !m["reasoning_content"].get<std::string>().empty()) {
                ContentBlock b;
                b.kind = ContentKind::Thinking;
                b.text = m["reasoning_content"].get<std::string>();
                msg.content.push_back(std::move(b));
            }
            if (m.contains("reasoning") && m["reasoning"].is_object() &&
                m["reasoning"].contains("content") &&
                m["reasoning"]["content"].is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Thinking;
                b.text = m["reasoning"]["content"].get<std::string>();
                msg.content.push_back(std::move(b));
            }

            // Normalize: role=system content becomes top-level system blocks.
            if (role == "system") {
                for (auto &b : msg.content) {
                    if (b.kind == ContentKind::Text) {
                        ContentBlock sys;
                        sys.kind = ContentKind::Text;
                        sys.text = b.text;
                        out.system.push_back(std::move(sys));
                    }
                }
            } else {
                out.messages.push_back(std::move(msg));
            }
        }
    }

    if (in.contains("tools") && in["tools"].is_array()) {
        for (const auto &t : in["tools"]) {
            if (!t.is_object()) continue;
            Tool tool;
            const json *fn = &t;
            if (t.contains("type") && t["type"].is_string())
                tool.extra["type"] = t["type"];
            if (t.contains("function") && t["function"].is_object())
                fn = &t["function"];
            if (fn->contains("name") && (*fn)["name"].is_string())
                tool.name = (*fn)["name"].get<std::string>();
            if (fn->contains("description") && (*fn)["description"].is_string())
                tool.description = (*fn)["description"].get<std::string>();
            if (fn->contains("parameters") && (*fn)["parameters"].is_object())
                tool.input_schema = (*fn)["parameters"];
            out.tools.push_back(std::move(tool));
        }
    }
    if (in.contains("tool_choice"))
        out.tool_choice = in["tool_choice"];

    if (in.contains("reasoning_effort") && in["reasoning_effort"].is_string()) {
        out.reasoning.enabled = true;
        out.reasoning.effort = in["reasoning_effort"].get<std::string>();
    }
    if (in.contains("reasoning") && in["reasoning"].is_object()) {
        if (in["reasoning"].contains("effort") && in["reasoning"]["effort"].is_string()) {
            out.reasoning.enabled = true;
            out.reasoning.effort = in["reasoning"]["effort"].get<std::string>();
        }
        out.reasoning.extra = in["reasoning"];
    }

    if (in.contains("max_completion_tokens") && in["max_completion_tokens"].is_number_integer())
        out.max_tokens = in["max_completion_tokens"].get<int>();
    else if (in.contains("max_tokens") && in["max_tokens"].is_number_integer())
        out.max_tokens = in["max_tokens"].get<int>();
    if (in.contains("temperature") && in["temperature"].is_number())
        out.temperature = in["temperature"].get<double>();

    if (in.contains("stop")) {
        const json &st = in["stop"];
        if (st.is_string()) {
            out.stop_sequences.push_back(st.get<std::string>());
        } else if (st.is_array()) {
            for (const auto &s : st)
                if (s.is_string()) out.stop_sequences.push_back(s.get<std::string>());
        }
    }

    // Collect unknown top-level keys into extras.
    for (const auto &it : in.items()) {
        bool consumed = false;
        for (const char *k : kConsumed) {
            if (it.key() == k) { consumed = true; break; }
        }
        if (!consumed) out.extras[it.key()] = it.value();
    }
    return true;
}

json OpenAICodec::serialize_request(const ir::ChatRequest &in) const {
    json body = fmt::filter_keys(
        in.extras, {"seed", "user", "n", "top_p", "presence_penalty",
                    "frequency_penalty", "logprobs", "top_logprobs",
                    "response_format", "metadata", "store", "service_tier",
                    "parallel_tool_calls", "web_search_options", "text"});

    body["model"] = in.model;
    body["stream"] = in.stream;
    if (in.stream) body["stream_options"]["include_usage"] = true;

    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = fmt::normalize_tool_choice_to_openai(in.tool_choice);
    if (in.reasoning.enabled && !in.reasoning.effort.empty())
        body["reasoning_effort"] = in.reasoning.effort;
    else if (in.reasoning.enabled && !in.reasoning.extra.empty() &&
             in.reasoning.extra.is_object() && in.reasoning.extra.contains("effort"))
        body["reasoning"] = in.reasoning.extra;
    if (in.max_tokens.has_value())
        body["max_completion_tokens"] = *in.max_tokens;
    if (in.temperature.has_value())
        body["temperature"] = *in.temperature;
    if (!in.stop_sequences.empty()) {
        body["stop"] = in.stop_sequences.size() == 1
                           ? json(in.stop_sequences[0])
                           : json(in.stop_sequences);
    }

    if (!in.tools.empty()) {
        json arr = json::array();
        for (const auto &t : in.tools) {
            json j;
            if (t.extra.contains("type") && t.extra["type"].is_string())
                j["type"] = t.extra["type"];
            else
                j["type"] = "function";
            j["function"] = json::object();
            if (!t.name.empty()) j["function"]["name"] = t.name;
            if (!t.description.empty()) j["function"]["description"] = t.description;
            if (t.input_schema.is_object()) j["function"]["parameters"] = t.input_schema;
            arr.push_back(std::move(j));
        }
        body["tools"] = std::move(arr);
    }

    if (!in.system.empty()) {
        std::string sys_text;
        for (const auto &b : in.system)
            if (b.kind == ContentKind::Text) sys_text += b.text;
        if (!sys_text.empty()) {
            json sys = json::object();
            sys["role"] = "system";
            sys["content"] = sys_text;
            // Insert system first if body already has messages.
            if (body.contains("messages") && body["messages"].is_array())
                body["messages"].insert(body["messages"].begin(), std::move(sys));
            else
                body["messages"] = json::array({std::move(sys)});
        }
    }

    // System message was inserted into body["messages"] above (if any); keep it
    // and append the remaining IR messages.
    json msgs = body.contains("messages") && body["messages"].is_array()
                    ? std::move(body["messages"])
                    : json::array();

    // Append one content block to the OpenAI parts/tool_calls/reasoning outputs.
    // ToolResult is handled by callers (needs role:"tool" + tool_call_id).
    auto append_content_part = [](json &parts, json &tool_calls, bool &has_tools,
                                  std::string &reasoning_text,
                                  const ContentBlock &b) {
        switch (b.kind) {
            case ContentKind::Text: {
                if (b.extra.contains("raw") && b.extra["raw"].is_object()) {
                    parts.push_back(b.extra["raw"]);
                } else {
                    json p;
                    p["type"] = "text";
                    p["text"] = b.text;
                    parts.push_back(std::move(p));
                }
                break;
            }
            case ContentKind::Image: {
                parts.push_back(fmt::image_block_to_openai_part(b));
                break;
            }
            case ContentKind::Thinking: {
                if (b.extra.contains("raw") && b.extra["raw"].is_object())
                    parts.push_back(b.extra["raw"]);
                else
                    reasoning_text += b.text;
                break;
            }
            case ContentKind::ToolUse: {
                has_tools = true;
                json tc;
                if (!b.tool_call_id.empty()) tc["id"] = b.tool_call_id;
                tc["type"] = b.extra.contains("type") ? b.extra["type"].get<std::string>()
                                                       : std::string("function");
                tc["function"] = json::object();
                tc["function"]["name"] = b.tool_name;
                tc["function"]["arguments"] = b.tool_input.dump();
                tool_calls.push_back(std::move(tc));
                break;
            }
            default:
                break;  // ToolResult handled by callers
        }
    };

    for (const auto &m : in.messages) {
        json jm;
        jm["role"] = m.role;

        bool has_tr = false;
        for (const auto &b : m.content)
            if (b.kind == ContentKind::ToolResult) { has_tr = true; break; }

        if (has_tr) {
            // Anthropic carries tool results in role:"user" messages; OpenAI
            // requires role:"tool" + tool_call_id so the upstream can correlate
            // each result to the assistant's earlier tool_call. Split the message
            // into one role:"tool" message per result id (non-result parts keep
            // a normal message).
            json parts = json::array();
            bool has_tools = false;
            json tool_calls = json::array();
            std::string reasoning_text;
            std::vector<std::pair<std::string, std::string>> tr_by_id;
            for (const auto &b : m.content) {
                if (b.kind == ContentKind::ToolResult) {
                    if (!b.tool_use_id.empty()) {
                        bool found = false;
                        for (auto &kv : tr_by_id) {
                            if (kv.first == b.tool_use_id) {
                                kv.second += b.text;
                                found = true;
                                break;
                            }
                        }
                        if (!found) tr_by_id.emplace_back(b.tool_use_id, b.text);
                    } else {
                        // No id → cannot be a tool message; keep as text part.
                        json p;
                        p["type"] = "text";
                        p["text"] = b.text;
                        parts.push_back(std::move(p));
                    }
                    continue;
                }
                append_content_part(parts, tool_calls, has_tools, reasoning_text, b);
            }
            // Emit role:"tool" messages FIRST so they immediately follow the
            // assistant message that made the tool calls (OpenAI requires tool
            // messages to directly succeed the tool_calls assistant message;
            // interposing a user message → 400 on strict-compatible upstreams).
            for (const auto &kv : tr_by_id) {
                json jt;
                jt["role"] = "tool";
                jt["tool_call_id"] = kv.first;
                jt["content"] = kv.second;
                msgs.push_back(std::move(jt));
            }
            // Non-tool parts become a normal user message after the tool results.
            if (parts.size() == 1 && parts[0].value("type", "") == "text")
                jm["content"] = parts[0]["text"];
            else if (!parts.empty())
                jm["content"] = std::move(parts);
            if (has_tools && !tool_calls.empty())
                jm["tool_calls"] = std::move(tool_calls);
            if (!reasoning_text.empty())
                jm["reasoning_content"] = reasoning_text;
            if (jm.contains("content") || jm.contains("tool_calls") ||
                jm.contains("reasoning_content"))
                msgs.push_back(std::move(jm));
            continue;
        }

        // Content parts (no tool results in this message).
        json parts = json::array();
        bool has_tools = false;
        json tool_calls = json::array();
        std::string reasoning_text;
        for (const auto &b : m.content)
            append_content_part(parts, tool_calls, has_tools, reasoning_text, b);
        if (parts.size() == 1 && parts[0].value("type", "") == "text") {
            jm["content"] = parts[0]["text"];
        } else if (!parts.empty()) {
            jm["content"] = std::move(parts);
        }
        if (has_tools && !tool_calls.empty())
            jm["tool_calls"] = std::move(tool_calls);
        if (!reasoning_text.empty())
            jm["reasoning_content"] = reasoning_text;
        if (has_tools && parts.empty())
            jm["content"] = nullptr;  // explicit empty content for tool-only messages
        msgs.push_back(std::move(jm));
    }

    body["messages"] = std::move(msgs);
    return body;
}

// ── Response ────────────────────────────────────────────────────────────

bool OpenAICodec::parse_response(const json &in, ir::ChatResponse &out,
                                 std::string &err) const {
    if (!in.is_object()) {
        err = "OpenAI response must be a JSON object";
        return false;
    }
    if (in.contains("id") && in["id"].is_string())
        out.id = in["id"].get<std::string>();
    if (in.contains("model") && in["model"].is_string())
        out.model = in["model"].get<std::string>();

    if (in.contains("choices") && in["choices"].is_array() &&
        !in["choices"].empty() && in["choices"][0].is_object()) {
        const json &ch = in["choices"][0];
        if (ch.contains("finish_reason") && ch["finish_reason"].is_string())
            out.stop_reason = fmt::openai_finish_reason_to_stop(
                ch["finish_reason"].get<std::string>());
        if (ch.contains("message") && ch["message"].is_object()) {
            const json &msg = ch["message"];
            if (msg.contains("content")) {
                const json &c = msg["content"];
                if (c.is_string()) {
                    ContentBlock b;
                    b.kind = ContentKind::Text;
                    b.text = c.get<std::string>();
                    out.content.push_back(std::move(b));
                } else if (c.is_array()) {
                    for (const auto &part : c) {
                        if (!part.is_object()) continue;
                        std::string type = part.value("type", "");
                        if (type == "text" && part.contains("text") && part["text"].is_string()) {
                            ContentBlock b;
                            b.kind = ContentKind::Text;
                            b.text = part["text"].get<std::string>();
                            out.content.push_back(std::move(b));
                        } else {
                            ContentBlock b;
                            b.kind = ContentKind::Text;
                            b.extra["raw"] = part;
                            out.content.push_back(std::move(b));
                        }
                    }
                }
            }
            if (msg.contains("reasoning_content") && msg["reasoning_content"].is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Thinking;
                b.text = msg["reasoning_content"].get<std::string>();
                out.content.push_back(std::move(b));
            }
            if (msg.contains("reasoning") && msg["reasoning"].is_object() &&
                msg["reasoning"].contains("content") && msg["reasoning"]["content"].is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Thinking;
                b.text = msg["reasoning"]["content"].get<std::string>();
                out.content.push_back(std::move(b));
            }
            if (msg.contains("tool_calls") && msg["tool_calls"].is_array()) {
                for (const auto &tc : msg["tool_calls"]) {
                    if (!tc.is_object()) continue;
                    ContentBlock b;
                    b.kind = ContentKind::ToolUse;
                    if (tc.contains("id") && tc["id"].is_string())
                        b.tool_call_id = tc["id"].get<std::string>();
                    if (tc.contains("function") && tc["function"].is_object()) {
                        const json &fn = tc["function"];
                        if (fn.contains("name") && fn["name"].is_string())
                            b.tool_name = fn["name"].get<std::string>();
                        if (fn.contains("arguments")) {
                            std::string args =
                                fn["arguments"].is_string()
                                    ? fn["arguments"].get<std::string>()
                                    : fn["arguments"].dump();
                            try {
                                b.tool_input = json::parse(args);
                            } catch (...) {
                                b.tool_input = json::object();
                            }
                        }
                    }
                    out.content.push_back(std::move(b));
                }
            }
        }
    }

    if (in.contains("usage") && in["usage"].is_object()) {
        const json &u = in["usage"];
        if (u.contains("prompt_tokens") && u["prompt_tokens"].is_number_integer())
            out.usage.prompt_tokens = u["prompt_tokens"].get<int>();
        if (u.contains("completion_tokens") && u["completion_tokens"].is_number_integer())
            out.usage.completion_tokens = u["completion_tokens"].get<int>();
        if (u.contains("total_tokens") && u["total_tokens"].is_number_integer())
            out.usage.total_tokens = u["total_tokens"].get<int>();
        else
            out.usage.total_tokens = out.usage.prompt_tokens + out.usage.completion_tokens;
        if (u.contains("prompt_tokens_details") && u["prompt_tokens_details"].is_object() &&
            u["prompt_tokens_details"].contains("cached_tokens") &&
            u["prompt_tokens_details"]["cached_tokens"].is_number_integer())
            out.usage.cache_read_tokens = u["prompt_tokens_details"]["cached_tokens"].get<int>();
        if (u.contains("completion_tokens_details") && u["completion_tokens_details"].is_object())
            out.usage.extra["completion_tokens_details"] = u["completion_tokens_details"];
    }

    out.extras["created"] = in.contains("created") ? in["created"] : json(nullptr);
    if (in.contains("system_fingerprint")) out.extras["system_fingerprint"] = in["system_fingerprint"];
    if (in.contains("object")) out.extras["object"] = in["object"];
    return true;
}

json OpenAICodec::serialize_response(const ir::ChatResponse &in) const {
    json out;
    out["id"] = in.id.empty() ? "chatcmpl-proxy" : in.id;
    out["object"] = "chat.completion";  // forced — never inherit source format
    out["created"] = in.extras.contains("created") ? in.extras["created"]
                                                   : json(nullptr);
    out["model"] = in.model;
    if (in.extras.contains("system_fingerprint"))
        out["system_fingerprint"] = in.extras["system_fingerprint"];

    json msg;
    msg["role"] = "assistant";
    json content = json::array();
    json tool_calls = json::array();
    std::string reasoning_text;
    for (const auto &b : in.content) {
        switch (b.kind) {
            case ContentKind::Text: {
                if (b.extra.contains("raw") && b.extra["raw"].is_object())
                    content.push_back(b.extra["raw"]);
                else {
                    json p;
                    p["type"] = "text";
                    p["text"] = b.text;
                    content.push_back(std::move(p));
                }
                break;
            }
            case ContentKind::Image:
                content.push_back(fmt::image_block_to_openai_part(b));
                break;
            case ContentKind::Thinking:
                reasoning_text += b.text;
                break;
            case ContentKind::ToolUse: {
                json tc;
                if (!b.tool_call_id.empty()) tc["id"] = b.tool_call_id;
                tc["type"] = "function";
                tc["function"] = json::object();
                tc["function"]["name"] = b.tool_name;
                tc["function"]["arguments"] = b.tool_input.dump();
                tool_calls.push_back(std::move(tc));
                break;
            }
            case ContentKind::ToolResult:
                break;  // never in a response
        }
    }
    if (content.size() == 1 && content[0].value("type", "") == "text") {
        msg["content"] = content[0]["text"];
    } else if (!content.empty()) {
        msg["content"] = std::move(content);
    } else {
        msg["content"] = nullptr;
    }
    if (!reasoning_text.empty()) msg["reasoning_content"] = reasoning_text;
    if (!tool_calls.empty()) msg["tool_calls"] = std::move(tool_calls);

    json choice;
    choice["index"] = 0;
    choice["message"] = std::move(msg);
    choice["finish_reason"] = fmt::stop_reason_to_openai(in.stop_reason);
    out["choices"] = json::array({std::move(choice)});

    json usage;
    usage["prompt_tokens"] = in.usage.prompt_tokens;
    usage["completion_tokens"] = in.usage.completion_tokens;
    usage["total_tokens"] = in.usage.total_tokens;
    if (in.usage.cache_read_tokens > 0) {
        usage["prompt_tokens_details"] = json::object();
        usage["prompt_tokens_details"]["cached_tokens"] = in.usage.cache_read_tokens;
    }
    if (in.usage.extra.contains("completion_tokens_details"))
        usage["completion_tokens_details"] = in.usage.extra["completion_tokens_details"];
    out["usage"] = std::move(usage);
    return out;
}

// ── Streaming: parser (upstream OpenAI SSE → IR events) ─────────────────

class OpenAIStreamParser : public StreamParser {
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
            if (payload == "[DONE]") return;
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
            if (payload == "[DONE]") return;
            json j;
            try {
                j = json::parse(payload);
            } catch (...) {
                return;
            }
            handle_frame(j, guard);
        });
        if (ok) flush_tool_done(guard);
        return ok;
    }

private:
    struct ActiveTool {
        std::string id, name, arguments;
        bool start_emitted = false;
    };
    fmt::SseFrameBuffer sse_;
    std::map<int, ActiveTool> tool_calls_;
    std::string id_, model_;
    bool started_ = false;

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
                    auto &at = tool_calls_[tindex];
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
                return true;  // role chunk emitted lazily on first content
            case StreamEventType::ContentTextDelta:
                if (!started_ && !emit_role_chunk(sink)) return false;
                return sink("data: " + chunk({{"content", ev.text}}, ev.index).dump() + "\n\n");
            case StreamEventType::ContentThinkingDelta:
                if (!started_ && !emit_role_chunk(sink)) return false;
                return sink("data: " + chunk({{"reasoning_content", ev.text}}, ev.index).dump() + "\n\n");
            case StreamEventType::ToolCallStart: {
                if (!started_ && !emit_role_chunk(sink)) return false;
                json tc;
                tc["index"] = 0;
                tc["id"] = ev.text;
                tc["type"] = "function";
                tc["function"] = json::object();
                tc["function"]["name"] = ev.arguments;
                tc["function"]["arguments"] = "";
                json delta;
                delta["tool_calls"] = json::array({std::move(tc)});
                return sink("data: " + chunk(std::move(delta), ev.index).dump() + "\n\n");
            }
            case StreamEventType::ToolCallArgumentDelta: {
                if (!started_ && !emit_role_chunk(sink)) return false;
                json tc;
                tc["index"] = 0;
                tc["function"] = json::object();
                tc["function"]["arguments"] = ev.arguments;
                json delta;
                delta["tool_calls"] = json::array({std::move(tc)});
                return sink("data: " + chunk(std::move(delta), ev.index).dump() + "\n\n");
            }
            case StreamEventType::ToolCallDone:
                return true;  // no explicit done in OpenAI stream
            case StreamEventType::MessageFinish:
                deferred_finish_ = fmt::stop_reason_to_openai(ev.stop_reason);
                return true;  // emitted after usage (or at finish)
            case StreamEventType::UsageEvent:
                if (started_ && !deferred_finish_.empty() && !finish_emitted_) {
                    if (!emit_finish_chunk(sink)) return false;
                }
                return sink("data: " + usage_chunk(ev.usage).dump() + "\n\n");
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

std::unique_ptr<ir::StreamParser> OpenAICodec::make_stream_parser() const {
    return std::make_unique<OpenAIStreamParser>();
}
std::unique_ptr<ir::StreamEmitter> OpenAICodec::make_stream_emitter() const {
    return std::make_unique<OpenAIStreamEmitter>();
}

std::unique_ptr<FormatCodec> make_openai_codec() {
    return std::make_unique<OpenAICodec>();
}
