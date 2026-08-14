#include "format_openai_internal.h"

using namespace ir;

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
                    } else if (type == "file" || type == "input_audio" ||
                               type == "audio") {
                        fmt::parse_media_content(part, msg.content, false);
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
                fmt::parse_tool_result_content(
                    m.contains("content") ? m["content"] : json(nullptr), b);
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
                    out.system.push_back(std::move(b));
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
        if (!openai_request_key_consumed(it.key()))
            out.extras[it.key()] = it.value();
    }
    return true;
}

json OpenAICodec::serialize_request(const ir::ChatRequest &in) const {
    json body = fmt::filter_keys(
        in.extras, {"seed", "user", "n", "top_p", "presence_penalty",
                    "frequency_penalty", "logprobs", "top_logprobs",
                    "response_format", "metadata", "store", "service_tier",
                    "parallel_tool_calls", "web_search_options", "text",
                    "stream_options"});

    body["model"] = in.model;
    body["stream"] = in.stream;

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
            std::string type =
                t.extra.contains("type") && t.extra["type"].is_string()
                    ? t.extra["type"].get<std::string>()
                    : std::string("function");
            if (type == "custom") {
                // Codex custom tool (apply_patch) → regular function with a
                // raw-string input parameter (mirror cc-switch
                // transform_codex_chat.rs add_custom_tool). The original tool
                // definition is embedded in the description so the model can
                // reproduce the freeform payload.
                json j;
                j["type"] = "function";
                j["function"]["name"] = t.name;
                std::string desc = t.description;
                if (t.extra.contains("raw") && t.extra["raw"].is_object()) {
                    desc += "\n\nOriginal tool definition:\n```json\n" +
                            t.extra["raw"].dump() + "\n```";
                }
                j["function"]["description"] = desc;
                j["function"]["parameters"] = json{
                    {"type", "object"},
                    {"properties",
                     json{{"input",
                           json{{"type", "string"},
                                {"description",
                                 "Raw string input for the original custom "
                                 "tool. Preserve formatting exactly and follow "
                                 "the original tool definition embedded in the "
                                 "description."}}}}},
                    {"required", json::array({"input"})}};
                arr.push_back(std::move(j));
            } else if (type != "function") {
                // namespace / web_search / tool_search are not representable
                // in OpenAI chat completions; drop them (cc-switch drops
                // web_search; namespace children are not preserved by the IR).
                continue;
            } else {
                json j;
                j["type"] = "function";
                j["function"] = json::object();
                if (!t.name.empty()) j["function"]["name"] = t.name;
                if (!t.description.empty())
                    j["function"]["description"] = t.description;
                if (t.input_schema.is_object())
                    j["function"]["parameters"] =
                        fmt::normalize_function_parameters(t.input_schema);
                arr.push_back(std::move(j));
            }
        }
        body["tools"] = std::move(arr);
    }

    if (!in.system.empty()) {
        std::string sys_text;
        for (const auto &b : in.system)
            if (b.kind == ContentKind::Text) sys_text += b.text;
        if (!in.system.empty()) {
            json sys = json::object();
            sys["role"] = "system";
            bool only_text = true;
            for (const auto &b : in.system)
                only_text = only_text && b.kind == ContentKind::Text;
            sys["content"] = only_text ? json(sys_text)
                                        : fmt::serialize_openai_content_blocks(in.system);
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
            case ContentKind::File:
                parts.push_back(fmt::serialize_openai_file_part(b));
                break;
            case ContentKind::Audio:
                parts.push_back(fmt::serialize_openai_audio_part(b));
                break;
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
        // OpenAI chat completions has no `developer` role; Codex sends
        // `role:"developer"` instruction messages that strict upstreams
        // (opencode.ai Console Go) reject with 400. Map to `system` exactly
        // like cc-switch's `responses_role_to_chat_role`.
        jm["role"] = m.role == "developer" ? "system" : m.role;

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
            json tool_media = json::array();
            std::string reasoning_text;
            std::vector<std::pair<std::string, std::string>> tr_by_id;
            for (const auto &b : m.content) {
                if (b.kind == ContentKind::ToolResult) {
                    if (!b.tool_use_id.empty()) {
                        bool found = false;
                        for (auto &kv : tr_by_id) {
                            if (kv.first == b.tool_use_id) {
                                kv.second += fmt::tool_result_text(b);
                                found = true;
                                break;
                            }
                        }
                        if (!found) tr_by_id.emplace_back(b.tool_use_id, fmt::tool_result_text(b));
                        json media = fmt::serialize_openai_tool_media_parts(b);
                        for (const auto &part : media) tool_media.push_back(part);
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
            if (!tool_media.empty())
                msgs.push_back(json{{"role", "user"}, {"content", tool_media}});
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
        bool has_tool_calls = has_tools && !tool_calls.empty();
        if (has_tool_calls)
            jm["tool_calls"] = std::move(tool_calls);
        // Reasoning-vendor upstreams (DeepSeek/Moonshot/Kimi/Mimo) reject
        // assistant tool_calls messages that lack `reasoning_content`
        // (opencode.ai "Console Go" → 400).  Mirror cc-switch's
        // preserve_reasoning_content: use the message's thinking text when
        // present, else the "tool call" placeholder.
        if (m.role == "assistant" && has_tool_calls && fmt::is_reasoning_vendor(in.model))
            jm["reasoning_content"] = reasoning_text.empty() ? "tool call"
                                                             : reasoning_text;
        else if (!reasoning_text.empty())
            jm["reasoning_content"] = reasoning_text;
        if (has_tools && parts.empty())
            jm["content"] = nullptr;  // explicit empty content for tool-only messages
        msgs.push_back(std::move(jm));
    }

    body["messages"] = std::move(msgs);

    // OpenAI-compatible upstreams (MiniMax/DeepSeek/…) reject `system`
    // messages anywhere but the first slot; Codex produces several
    // (instructions + one per `developer` turn).  Merge every system message
    // into a single leading one — mirroring cc-switch's
    // `collapse_system_messages_to_head`.
    collapse_openai_system_messages(body["messages"]);
    return body;
}

// ── Response ────────────────────────────────────────────────────────────
