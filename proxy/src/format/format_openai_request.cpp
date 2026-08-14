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
                            if (!fn["arguments"].is_string()) {
                                err = "tool call arguments must be a JSON string";
                                return false;
                            }
                            std::string args = fn["arguments"].get<std::string>();
                            b.extra["raw_arguments"] = args;
                            try { b.tool_input = json::parse(args); }
                            catch (...) { err = "invalid JSON in tool call arguments"; return false; }
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
                    if (!fc["arguments"].is_string()) {
                        err = "function_call arguments must be a JSON string";
                        return false;
                    }
                    std::string args = fc["arguments"].get<std::string>();
                    b.extra["raw_arguments"] = args;
                    try { b.tool_input = json::parse(args); }
                    catch (...) { err = "invalid JSON in function_call arguments"; return false; }
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
                msg.content.insert(msg.content.begin(), std::move(b));
            }
            if (m.contains("reasoning") && m["reasoning"].is_object() &&
                m["reasoning"].contains("content") &&
                m["reasoning"]["content"].is_string()) {
                ContentBlock b;
                b.kind = ContentKind::Thinking;
                b.text = m["reasoning"]["content"].get<std::string>();
                msg.content.insert(msg.content.begin(), std::move(b));
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
            if (t.contains("type") && t["type"].is_string()) {
                tool.extra["type"] = t["type"];
                tool.wire_type = t["type"].get<std::string>();
                if (tool.wire_type == "custom") tool.kind = ToolKind::Custom;
                else if (tool.wire_type == "tool_search") tool.kind = ToolKind::ToolSearch;
                else if (tool.wire_type == "namespace") tool.kind = ToolKind::Namespace;
                else if (tool.wire_type != "function") tool.kind = ToolKind::Hosted;
            }
            if (t.contains("function") && t["function"].is_object())
                fn = &t["function"];
            if (fn->contains("name") && (*fn)["name"].is_string())
                tool.name = (*fn)["name"].get<std::string>();
            if (fn->contains("description") && (*fn)["description"].is_string())
                tool.description = (*fn)["description"].get<std::string>();
            if (fn->contains("parameters") && (*fn)["parameters"].is_object())
                tool.input_schema = (*fn)["parameters"];
            if (tool.kind == ToolKind::Namespace) parse_openai_namespace_children(t, tool);
            tool.raw = t;
            out.tools.push_back(std::move(tool));
        }
    }
    out.items = out.messages;
    if (in.contains("tool_choice"))
        out.tool_choice = in["tool_choice"];

    if (in.contains("response_format") && in["response_format"].is_object()) {
        const auto &rf = in["response_format"];
        out.structured_output.raw = rf;
        const std::string type = rf.value("type", "text");
        if (type == "json_schema") {
            out.structured_output.kind = StructuredOutputKind::JsonSchema;
            const auto &schema = rf.contains("json_schema") &&
                                         rf["json_schema"].is_object()
                                     ? rf["json_schema"] : rf;
            out.structured_output.name = schema.value("name", "");
            out.structured_output.description = schema.value("description", "");
            if (schema.contains("schema")) out.structured_output.schema = schema["schema"];
            out.structured_output.strict = schema.value("strict", false);
        } else if (type == "json_object") {
            out.structured_output.kind = StructuredOutputKind::JsonObject;
        } else {
            out.structured_output.kind = StructuredOutputKind::Text;
        }
    }

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

json OpenAICodec::serialize_request(const ir::ChatRequest &in,
                                    const ir::ConversionContext *context) const {
    json body = fmt::filter_keys(
        in.extras, {"seed", "user", "n", "top_p", "presence_penalty",
                    "frequency_penalty", "logprobs", "top_logprobs",
                    "response_format", "metadata", "store", "service_tier",
                    "parallel_tool_calls", "web_search_options",
                    "stream_options"});
    body["model"] = in.model;
    body["stream"] = in.stream;
    if (in.structured_output.kind == StructuredOutputKind::JsonObject) {
        body["response_format"] = json{{"type", "json_object"}};
    } else if (in.structured_output.kind == StructuredOutputKind::JsonSchema) {
        json schema{{"name", in.structured_output.name},
                    {"description", in.structured_output.description},
                    {"schema", in.structured_output.schema},
                    {"strict", in.structured_output.strict}};
        body["response_format"] = json{{"type", "json_schema"},
                                        {"json_schema", std::move(schema)}};
    } else if (in.structured_output.raw.is_object() &&
               !in.structured_output.raw.empty()) {
        body["response_format"] = in.structured_output.raw;
    }

    if (!in.tool_choice.is_null() && !in.tool_choice.empty())
        body["tool_choice"] = fmt::normalize_tool_choice_for_target(
            in.tool_choice, context, ApiFormat::OpenAI);
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

    const auto &source_items = in.items.empty() ? in.messages : in.items;
    // A Chat assistant turn is represented by several ordered Agent Items
    // (reasoning, visible text and parallel calls).  Chat Completions has one
    // assistant message for that turn, so merge only continuous Items carrying
    // the same non-zero group id and role.  Items without a group id retain
    // their original message boundaries.
    std::vector<Message> merged_items;
    merged_items.reserve(source_items.size());
    for (const auto &item : source_items) {
        if (!merged_items.empty() && item.group_id != 0 &&
            merged_items.back().group_id == item.group_id &&
            merged_items.back().role == item.role) {
            auto &merged = merged_items.back();
            merged.content.insert(merged.content.end(), item.content.begin(),
                                  item.content.end());
            continue;
        }
        merged_items.push_back(item);
    }
    for (const auto &m : merged_items) {
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
                append_openai_content_part(parts, tool_calls, has_tools, reasoning_text, b);
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
            append_openai_content_part(parts, tool_calls, has_tools, reasoning_text, b);
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
        // Encrypted-only Responses reasoning is intentionally kept in the
        // local Responses state chain; Chat has no opaque reasoning carrier.
        // Do not manufacture an empty assistant message for it.
        if (m.item_kind == ItemKind::Reasoning && parts.empty() &&
            !has_tool_calls && reasoning_text.empty())
            continue;
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
