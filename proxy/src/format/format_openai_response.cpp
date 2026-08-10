#include "format_openai_internal.h"

using namespace ir;

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
        out.usage.cache_read_tokens =
            fmt::read_cache_hit_tokens(u, out.usage.prompt_tokens).value_or(0);
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
