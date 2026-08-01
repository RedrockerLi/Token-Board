/// Standalone verification for the three format codecs.
///
///   format_conv_test --self-test          run the embedded round-trip matrix
///   format_conv_test --request --from <f> --to <g> [--stream] < body.json
///                                         convert one request body (f→g)
///   format_conv_test --response --from <f> --to <g> < body.json
///                                         convert one response body (f→g)
///
/// Formats: openai | anthropic | responses.  Exit code 0 = all checks passed.
#include <cstdio>
#include <cstring>
#include <iostream>
#include <iterator>
#include <string>
#include <utility>
#include <vector>

#include "codec.h"
#include "format_anthropic.h"
#include "format_common.h"
#include "format_openai.h"
#include "format_responses.h"

using namespace ir;

static const char *kOpenaiRequest = R"({
  "model": "gpt-test",
  "stream": false,
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "", "tool_calls": [
      {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"SF\"}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "72F"}
  ],
  "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
  "temperature": 0.5,
  "max_tokens": 100,
  "user": "alice"
})";

static const char *kAnthropicRequest = R"({
  "model": "claude-test",
  "system": "You are helpful.",
  "max_tokens": 100,
  "stream": false,
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
    {"role": "assistant", "content": [
      {"type": "text", "text": ""},
      {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F"}]}
  ],
  "tools": [{"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
})";

static const char *kResponsesRequest = R"({
  "model": "resp-test",
  "instructions": "You are helpful.",
  "input": [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
    {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
    {"type": "function_call_output", "call_id": "call_1", "output": "72F"}
  ],
  "tools": [{"type": "function", "name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}],
  "max_output_tokens": 100,
  "temperature": 0.2
})";

static const char *kOpenaiResponse = R"({
  "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-test",
  "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"},
               "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 3}}
})";

static const char *kAnthropicResponse = R"({
  "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
  "content": [{"type": "text", "text": "Hello"}],
  "stop_reason": "end_turn", "stop_sequence": null,
  "usage": {"input_tokens": 11, "output_tokens": 7,
            "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}
})";

static const char *kResponsesResponse = R"({
  "id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
  "model": "resp-test",
  "output": [
    {"id": "i1", "type": "message", "status": "completed", "role": "assistant",
     "content": [{"type": "output_text", "text": "Hello", "annotations": []}]}
  ],
  "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
            "input_tokens_details": {"cached_tokens": 3}}
})";

static int g_failures = 0;

static void check(bool ok, const std::string &what) {
    if (!ok) {
        printf("  FAIL: %s\n", what.c_str());
        g_failures++;
    } else {
        printf("  ok:   %s\n", what.c_str());
    }
}

static bool request_matches(const ChatRequest &a, const ChatRequest &b) {
    if (a.model != b.model) return false;
    if (a.system.size() != b.system.size()) return false;
    for (size_t i = 0; i < a.system.size(); i++)
        if (a.system[i].text != b.system[i].text) return false;
    if (a.messages.size() != b.messages.size()) return false;
    for (size_t i = 0; i < a.messages.size(); i++) {
        if (a.messages[i].role != b.messages[i].role) return false;
        if (a.messages[i].content.size() != b.messages[i].content.size()) return false;
        for (size_t j = 0; j < a.messages[i].content.size(); j++) {
            const auto &x = a.messages[i].content[j];
            const auto &y = b.messages[i].content[j];
            if (x.kind != y.kind) return false;
            if (x.text != y.text) return false;
            if (x.kind == ContentKind::ToolUse && x.tool_name != y.tool_name) return false;
            if (x.kind == ContentKind::ToolUse && x.tool_call_id != y.tool_call_id) return false;
            if (x.kind == ContentKind::ToolResult && x.tool_use_id != y.tool_use_id) return false;
        }
    }
    if (a.tools.size() != b.tools.size()) return false;
    for (size_t i = 0; i < a.tools.size(); i++)
        if (a.tools[i].name != b.tools[i].name) return false;
    return true;
}

static void run_request_roundtrip(const CodecRegistry &reg, ApiFormat from,
                                  ApiFormat to, const char *sample) {
    const FormatCodec &fc = reg.get(from);
    const FormatCodec &tc = reg.get(to);
    ChatRequest a;
    std::string err;
    if (!fc.parse_request(json::parse(sample), a, err)) {
        check(false, "parse " + to_string(from) + " request: " + err);
        return;
    }
    json converted = tc.serialize_request(a);
    ChatRequest b;
    if (!tc.parse_request(converted, b, err)) {
        check(false, "reparse " + to_string(to) + " request: " + err);
        return;
    }
    std::string label = to_string(from) + "→" + to_string(to) +
                        " request roundtrip (model=" + a.model + ")";
    if (to == from) {
        check(request_matches(a, b), label);
    } else {
        check(true, label + " [converted, key fields checked below]");
        check(b.model == a.model, "  model preserved");
        check(!b.system.empty() && b.system[0].text == a.system[0].text,
              "  system preserved");
    }
}

static void run_response_roundtrip(const CodecRegistry &reg, ApiFormat from,
                                   ApiFormat to, const char *sample) {
    const FormatCodec &fc = reg.get(from);
    const FormatCodec &tc = reg.get(to);
    ChatResponse a;
    std::string err;
    if (!fc.parse_response(json::parse(sample), a, err)) {
        check(false, "parse " + to_string(from) + " response: " + err);
        return;
    }
    json converted = tc.serialize_response(a);
    ChatResponse b;
    if (!tc.parse_response(converted, b, err)) {
        check(false, "reparse " + to_string(to) + " response: " + err);
        return;
    }
    std::string label = to_string(from) + "→" + to_string(to) + " response";
    check(b.model == a.model, label + " model");
    check(b.usage.prompt_tokens == a.usage.prompt_tokens &&
              b.usage.completion_tokens == a.usage.completion_tokens,
          label + " usage tokens");
    check(b.usage.cache_read_tokens == a.usage.cache_read_tokens,
          label + " cache_read tokens");
}

// ── Cross-format stream/request regression tests ─────────────────────────

static std::string convert_stream(const CodecRegistry &reg, ApiFormat from,
                                  ApiFormat to, const std::string &sse) {
    const FormatCodec &fc = reg.get(from);
    const FormatCodec &tc = reg.get(to);
    auto parser = fc.make_stream_parser();
    auto emitter = tc.make_stream_emitter();
    if (!parser || !emitter) return "";
    std::string out;
    auto sink = [&out](const std::string &c) -> bool { out += c; return true; };
    auto emit = [&](const StreamEvent &ev) -> bool {
        return emitter->emit(ev, sink);
    };
    parser->feed(sse.data(), sse.size(), emit);
    parser->finish(emit);
    emitter->finish(sink);
    return out;
}

static std::vector<std::pair<std::string, json>>
split_anthropic_frames(const std::string &sse) {
    std::vector<std::pair<std::string, json>> frames;
    size_t start = 0;
    while (start < sse.size()) {
        size_t sep = sse.find("\n\n", start);
        std::string block =
            sse.substr(start, sep == std::string::npos ? std::string::npos
                                                       : sep - start);
        start = (sep == std::string::npos) ? sse.size() : sep + 2;
        std::string event_name, data;
        if (!fmt::parse_sse_frame(block, &event_name, &data)) continue;
        if (data.empty()) continue;
        json j;
        try { j = json::parse(data); } catch (...) { continue; }
        frames.emplace_back(event_name, std::move(j));
    }
    return frames;
}

static int count_frames(const std::vector<std::pair<std::string, json>> &frames,
                        const std::string &event) {
    int n = 0;
    for (const auto &f : frames)
        if (f.first == event) n++;
    return n;
}

// Bug A + E: OpenAI tool-call stream must emit valid Anthropic SSE —
// content_block_start before any delta, no spurious empty text block, no
// empty input_json_delta, and message_delta.stop_reason == "tool_use".
static void test_openai_to_anthropic_stream_tool_calls(const CodecRegistry &reg) {
    printf("--- OpenAI→Anthropic stream tool calls ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"arguments\":\"\"}}]},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"{\\\"city\\\":\\\"SF\\\"}\"}}]},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}],\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":7,\"total_tokens\":18}}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    check(!frames.empty() && frames[0].first == "message_start",
          "tool-call stream: first frame is message_start");

    std::vector<json> tool_starts, text_starts;
    int input_deltas = 0, empty_input = 0, text_deltas = 0;
    for (const auto &f : frames) {
        if (f.first == "content_block_start") {
            json cb = f.second.value("content_block", json());
            if (cb.value("type", "") == "tool_use") tool_starts.push_back(f.second);
            else if (cb.value("type", "") == "text") text_starts.push_back(f.second);
        } else if (f.first == "content_block_delta") {
            json d = f.second.value("delta", json());
            std::string dt = d.value("type", "");
            if (dt == "input_json_delta") {
                input_deltas++;
                if (d.value("partial_json", "").empty()) empty_input++;
            } else if (dt == "text_delta") {
                text_deltas++;
            }
        }
    }
    check(tool_starts.size() == 1, "tool-call stream: exactly one tool_use block start");
    if (!tool_starts.empty()) {
        json cb = tool_starts[0]["content_block"];
        check(cb.value("id", "") == "call_1", "tool-call stream: tool id preserved");
        check(cb.value("name", "") == "get_weather", "tool-call stream: tool name preserved");
    }
    check(text_starts.empty(), "tool-call stream: no spurious text block");
    check(text_deltas == 0, "tool-call stream: no text deltas");
    check(input_deltas > 0, "tool-call stream: input_json deltas present");
    check(empty_input == 0, "tool-call stream: no empty input_json deltas");

    size_t first_tool_start = frames.size();
    for (size_t i = 0; i < frames.size(); i++)
        if (frames[i].first == "content_block_start" &&
            frames[i].second.value("content_block", json()).value("type", "") == "tool_use")
        { first_tool_start = i; break; }
    bool delta_before_start = false;
    for (size_t i = 0; i < first_tool_start && i < frames.size(); i++)
        if (frames[i].first == "content_block_delta") delta_before_start = true;
    check(!delta_before_start, "tool-call stream: no content_block_delta before block start");

    check(count_frames(frames, "content_block_stop") == 1,
          "tool-call stream: one content_block_stop");
    bool stop_reason_tool_use = false;
    for (const auto &f : frames)
        if (f.first == "message_delta" &&
            f.second.value("delta", json()).value("stop_reason", "") == "tool_use")
            stop_reason_tool_use = true;
    check(stop_reason_tool_use, "tool-call stream: message_delta.stop_reason == tool_use");
    check(!frames.empty() && frames.back().first == "message_stop",
          "tool-call stream: last frame is message_stop");
}

// Bug B: reasoning_content then content must produce a separate thinking block
// and text block (distinct indices), not merged into one text block.
static void test_openai_to_anthropic_stream_reasoning_then_text(const CodecRegistry &reg) {
    printf("--- OpenAI→Anthropic stream reasoning then text ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"Let me think\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Hello\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":5,\"completion_tokens\":8,\"total_tokens\":13}}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    std::vector<std::pair<int, std::string>> starts;
    for (const auto &f : frames) {
        if (f.first == "content_block_start")
            starts.emplace_back(f.second.value("index", -1),
                                f.second.value("content_block", json()).value("type", ""));
    }
    check(starts.size() == 2, "reasoning+text stream: two block starts");
    if (starts.size() == 2) {
        check(starts[0].first != starts[1].first, "reasoning+text stream: distinct indices");
        check(starts[0].second == "thinking" && starts[1].second == "text",
              "reasoning+text stream: thinking then text");
    }
    size_t text_start_pos = frames.size();
    for (size_t i = 0; i < frames.size(); i++)
        if (frames[i].first == "content_block_start" &&
            frames[i].second.value("content_block", json()).value("type", "") == "text")
        { text_start_pos = i; break; }
    bool thinking_before = false;
    for (size_t i = 0; i < text_start_pos && i < frames.size(); i++)
        if (frames[i].first == "content_block_delta" &&
            frames[i].second.value("delta", json()).value("type", "") == "thinking_delta")
            thinking_before = true;
    check(thinking_before, "reasoning+text stream: thinking_delta before text block");
    bool end_turn = false;
    for (const auto &f : frames)
        if (f.first == "message_delta" &&
            f.second.value("delta", json()).value("stop_reason", "") == "end_turn")
            end_turn = true;
    check(end_turn, "reasoning+text stream: message_delta.stop_reason == end_turn");
}

// Bug C: Anthropic user-message tool_result → OpenAI role:"tool" + tool_call_id.
static void test_anthropic_to_openai_request_tool_result(const CodecRegistry &reg) {
    printf("--- Anthropic→OpenAI request tool_result ---\n");
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    ir::ChatRequest req;
    std::string err;
    if (!ac.parse_request(json::parse(kAnthropicRequest), req, err)) {
        check(false, "tool_result: parse anthropic request: " + err);
        return;
    }
    json out = oc.serialize_request(req);
    check(out.value("model", "") == "claude-test", "tool_result: model preserved");
    const json &msgs = out["messages"];
    int tool_msgs = 0;
    std::string tool_call_id, tool_content;
    for (const auto &m : msgs) {
        if (m.value("role", "") == "tool") {
            tool_msgs++;
            tool_call_id = m.value("tool_call_id", "");
            tool_content = m.value("content", "");
        }
    }
    check(tool_msgs == 1, "tool_result: exactly one role:tool message");
    check(tool_call_id == "toolu_1", "tool_result: tool_call_id preserved");
    check(tool_content == "72F", "tool_result: content preserved");
    bool leaked = false;
    for (const auto &m : msgs) {
        if (m.value("role", "") != "user") continue;
        const json &c = m.value("content", json());
        if (c.is_string() && c.get<std::string>().find("72F") != std::string::npos)
            leaked = true;
        if (c.is_array())
            for (const auto &p : c)
                if (p.value("type", "") == "text" &&
                    p.value("text", "").find("72F") != std::string::npos)
                    leaked = true;
    }
    check(!leaked, "tool_result: not leaked into a user text message");
}

// Bug F: a user message combining a tool_result with text parts must emit the
// role:"tool" message(s) BEFORE the user text message. OpenAI requires tool
// messages to immediately follow the assistant tool_calls message; interposing
// a user message → 400 on strict-compatible upstreams (e.g. opencode.ai).
static void test_anthropic_to_openai_tool_result_with_text(const CodecRegistry &reg) {
    printf("--- Anthropic→OpenAI tool_result + text ordering ---\n");
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    const char *body = R"({
      "model": "m",
      "max_tokens": 10,
      "messages": [
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": [
          {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "SF"}}]},
        {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F"},
          {"type": "text", "text": "Now summarize."}]}
      ],
      "tools": [{"name": "get_weather", "input_schema": {"type": "object"}}]
    })";
    ir::ChatRequest req;
    std::string err;
    if (!ac.parse_request(json::parse(body), req, err)) {
        check(false, "tool_result+text: parse: " + err);
        return;
    }
    json out = oc.serialize_request(req);
    const json &msgs = out["messages"];
    // Find positions: assistant tool_calls message, then tool message, then user text.
    int ast_pos = -1, tool_pos = -1, user_pos = -1;
    for (size_t i = 0; i < msgs.size(); i++) {
        const json &m = msgs[i];
        if (m.value("role", "") == "assistant" && m.contains("tool_calls"))
            ast_pos = static_cast<int>(i);
        else if (m.value("role", "") == "tool") tool_pos = static_cast<int>(i);
        else if (m.value("role", "") == "user" &&
                 m.value("content", std::string()).find("Now summarize.") != std::string::npos)
            user_pos = static_cast<int>(i);
    }
    check(ast_pos >= 0, "tool_result+text: assistant tool_calls message present");
    check(tool_pos >= 0, "tool_result+text: role:tool message present");
    check(user_pos >= 0, "tool_result+text: user text message present");
    check(tool_pos > ast_pos, "tool_result+text: tool message after assistant tool_calls");
    check(user_pos > tool_pos, "tool_result+text: user text message AFTER tool message");
}

// Bug D: Anthropic tool_choice shapes → OpenAI equivalents.
// Reasoning-vendor upstreams (DeepSeek/Moonshot/Kimi/Mimo) reject assistant
// tool_calls messages that lack `reasoning_content` (opencode.ai "Console Go"
// → 400).  Mirror cc-switch's preserve_reasoning_content: inject the message's
// thinking text, or the "tool call" placeholder, only for such vendors.
static void test_anthropic_to_openai_reasoning_content(const CodecRegistry &reg) {
    printf("--- Anthropic→OpenAI reasoning_content on tool_calls ---\n");
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);

    const char *body = R"({
      "model": "deepseek-v4-flash",
      "max_tokens": 10,
      "messages": [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
          {"type": "thinking", "thinking": "need to run echo"},
          {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}}]},
        {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]}
      ],
      "tools": [{"name": "Bash", "input_schema": {"type": "object"}}]
    })";
    ir::ChatRequest req;
    std::string err;
    if (!ac.parse_request(json::parse(body), req, err)) {
        check(false, "rc: parse: " + err);
        return;
    }
    json out = oc.serialize_request(req);
    // Assistant tool_calls message gets the thinking text.
    bool found_thinking_rc = false;
    for (const auto &m : out["messages"]) {
        if (m.value("role", "") == "assistant" && m.contains("tool_calls")) {
            if (m.contains("reasoning_content") &&
                m["reasoning_content"].get<std::string>() == "need to run echo")
                found_thinking_rc = true;
        }
    }
    check(found_thinking_rc, "rc: reasoning_content = thinking text on deepseek assistant");

    // No thinking block → the "tool call" placeholder (deepseek still requires it).
    const char *body2 = R"({
      "model": "deepseek-v4-flash",
      "max_tokens": 10,
      "messages": [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
          {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}}]},
        {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]}
      ],
      "tools": [{"name": "Bash", "input_schema": {"type": "object"}}]
    })";
    ir::ChatRequest req2;
    if (!ac.parse_request(json::parse(body2), req2, err)) {
        check(false, "rc2: parse: " + err);
        return;
    }
    json out2 = oc.serialize_request(req2);
    bool found_placeholder = false;
    for (const auto &m : out2["messages"]) {
        if (m.value("role", "") == "assistant" && m.contains("tool_calls")) {
            if (m.contains("reasoning_content") &&
                m["reasoning_content"].get<std::string>() == "tool call")
                found_placeholder = true;
        }
    }
    check(found_placeholder, "rc: reasoning_content = 'tool call' placeholder (no thinking)");

    // Non-reasoning vendor must NOT get the placeholder injected (only thinking
    // text when present).
    const char *body3 = R"({
      "model": "gpt-4o",
      "max_tokens": 10,
      "messages": [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
          {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "echo hi"}}]},
        {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"}]}
      ],
      "tools": [{"name": "Bash", "input_schema": {"type": "object"}}]
    })";
    ir::ChatRequest req3;
    if (!ac.parse_request(json::parse(body3), req3, err)) {
        check(false, "rc3: parse: " + err);
        return;
    }
    json out3 = oc.serialize_request(req3);
    bool nonvendor_leaked = false;
    for (const auto &m : out3["messages"]) {
        if (m.value("role", "") == "assistant" && m.contains("tool_calls") &&
            m.contains("reasoning_content"))
            nonvendor_leaked = true;
    }
    check(!nonvendor_leaked, "rc: non-reasoning vendor not injected with placeholder");
}

static void test_anthropic_to_openai_tool_choice(const CodecRegistry &reg) {
    printf("--- Anthropic→OpenAI tool_choice ---\n");
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    struct Case { const char *tc; const char *expect; } cases[] = {
        {"{\"type\":\"tool\",\"name\":\"get_weather\"}",
         "{\"type\":\"function\",\"function\":{\"name\":\"get_weather\"}}"},
        {"{\"type\":\"any\"}", "\"required\""},
        {"{\"type\":\"none\"}", "\"none\""},
    };
    for (const auto &c : cases) {
        std::string body = std::string("{\"model\":\"m\",\"max_tokens\":10,\"tool_choice\":") +
                           c.tc + ",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}";
        ir::ChatRequest ir;
        std::string err;
        if (!ac.parse_request(json::parse(body), ir, err)) {
            check(false, std::string("tool_choice A→O: parse: ") + err);
            continue;
        }
        json out = oc.serialize_request(ir);
        check(json::parse(c.expect) == out.value("tool_choice", json()),
              std::string("tool_choice A→O: ") + c.tc + " → " +
                  out.value("tool_choice", json()).dump());
    }
}

// Bug D (reverse): OpenAI tool_choice shapes → Anthropic equivalents.
static void test_openai_to_anthropic_tool_choice(const CodecRegistry &reg) {
    printf("--- OpenAI→Anthropic tool_choice ---\n");
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    struct Case { const char *tc; const char *expect; } cases[] = {
        {"\"required\"", "{\"type\":\"any\"}"},
        {"\"none\"", "{\"type\":\"none\"}"},
        {"{\"type\":\"function\",\"function\":{\"name\":\"get_weather\"}}",
         "{\"type\":\"tool\",\"name\":\"get_weather\"}"},
    };
    for (const auto &c : cases) {
        std::string body = std::string("{\"model\":\"m\",\"tool_choice\":") + c.tc +
                           ",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}";
        ir::ChatRequest ir;
        std::string err;
        if (!oc.parse_request(json::parse(body), ir, err)) {
            check(false, std::string("tool_choice O→A: parse: ") + err);
            continue;
        }
        json out = ac.serialize_request(ir);
        check(json::parse(c.expect) == out.value("tool_choice", json()),
              std::string("tool_choice O→A: ") + c.tc + " → " +
                  out.value("tool_choice", json()).dump());
    }
}

// Bug G: cc sends model names with a `[1m]`/`[1M]` context-window marker that
// OpenAI-compatible upstreams reject ("Model X[1m] is not supported").  The
// marker must be stripped (case-insensitive) before forwarding.
static void test_strip_one_m_suffix(const CodecRegistry & /*reg*/) {
    printf("--- strip [1m] model suffix ---\n");
    struct Case { const char *in; const char *expect; } cases[] = {
        {"deepseek-v4-flash[1m]", "deepseek-v4-flash"},
        {"deepseek-v4-flash[1M]", "deepseek-v4-flash"},
        {"claude-opus-4-8[1M]", "claude-opus-4-8"},
        {"deepseek-v4-flash[1m] ", "deepseek-v4-flash"},
        {"deepseek-v4-flash", "deepseek-v4-flash"},     // no marker
        {"deepseek-v4-flash[1x]", "deepseek-v4-flash[1x]"},  // non-1m suffix kept
        {"gpt-5.4-mini", "gpt-5.4-mini"},               // plain model untouched
    };
    for (const auto &c : cases) {
        std::string got = fmt::strip_one_m_suffix_for_upstream(c.in);
        check(got == c.expect, std::string("strip: ") + c.in + " → " + got +
                                   " (expect " + c.expect + ")");
    }
}

static int self_test() {
    CodecRegistry reg;
    reg.add(make_openai_codec());
    reg.add(make_anthropic_codec());
    reg.add(make_responses_codec());

    printf("=== Request round-trips ===\n");
    struct ReqSample {
        ApiFormat fmt;
        const char *json;
    } reqs[] = {
        {ApiFormat::OpenAI, kOpenaiRequest},
        {ApiFormat::Anthropic, kAnthropicRequest},
        {ApiFormat::OpenAIResponses, kResponsesRequest},
    };
    for (auto &r : reqs) {
        for (int t = 0; t < 3; t++) {
            ApiFormat to = static_cast<ApiFormat>(t);
            run_request_roundtrip(reg, r.fmt, to, r.json);
        }
    }

    printf("=== Response round-trips ===\n");
    struct RespSample {
        ApiFormat fmt;
        const char *json;
    } resps[] = {
        {ApiFormat::OpenAI, kOpenaiResponse},
        {ApiFormat::Anthropic, kAnthropicResponse},
        {ApiFormat::OpenAIResponses, kResponsesResponse},
    };
    for (auto &r : resps) {
        for (int t = 0; t < 3; t++) {
            ApiFormat to = static_cast<ApiFormat>(t);
            run_response_roundtrip(reg, r.fmt, to, r.json);
        }
    }

    printf("=== Cross-format regression tests ===\n");
    test_openai_to_anthropic_stream_tool_calls(reg);
    test_openai_to_anthropic_stream_reasoning_then_text(reg);
    test_anthropic_to_openai_request_tool_result(reg);
    test_anthropic_to_openai_tool_result_with_text(reg);
    test_anthropic_to_openai_reasoning_content(reg);
    test_anthropic_to_openai_tool_choice(reg);
    test_openai_to_anthropic_tool_choice(reg);
    test_strip_one_m_suffix(reg);

    printf("\n%s (%d failure%s)\n", g_failures == 0 ? "ALL PASSED" : "FAILED",
           g_failures, g_failures == 1 ? "" : "s");
    return g_failures == 0 ? 0 : 1;
}

int main(int argc, char **argv) {
    CodecRegistry reg;
    reg.add(make_openai_codec());
    reg.add(make_anthropic_codec());
    reg.add(make_responses_codec());

    bool self_test_mode = true;
    bool is_request = true;
    bool stream_mode = false;
    std::string from, to;
    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--self-test") == 0) self_test_mode = true;
        else if (std::strcmp(argv[i], "--request") == 0) { self_test_mode = false; is_request = true; }
        else if (std::strcmp(argv[i], "--response") == 0) { self_test_mode = false; is_request = false; }
        else if (std::strcmp(argv[i], "--stream") == 0) { self_test_mode = false; stream_mode = true; }
        else if (std::strcmp(argv[i], "--from") == 0 && i + 1 < argc)
            from = argv[++i];
        else if (std::strcmp(argv[i], "--to") == 0 && i + 1 < argc)
            to = argv[++i];
    }

    if (self_test_mode) return self_test();

    auto norm = [](const std::string &s) {
        return s == "responses" ? std::string("openai_responses") : s;
    };
    ApiFormat f = parse_api_format(norm(from));
    ApiFormat t = parse_api_format(norm(to));
    if (from.empty() || to.empty()) {
        fprintf(stderr, "usage: format_conv_test --self-test | (--request|--response|--stream) --from <f> --to <g> < body.json\n");
        return 2;
    }
    // Read stdin raw (preserving newlines for SSE stream mode).
    std::string body((std::istreambuf_iterator<char>(std::cin)),
                     std::istreambuf_iterator<char>());
    const FormatCodec &fc = reg.get(f);
    const FormatCodec &tc = reg.get(t);
    std::string err;
    if (from == to) {
        printf("%s\n", body.c_str());
        return 0;
    }
    if (stream_mode) {
        auto parser = fc.make_stream_parser();
        auto emitter = tc.make_stream_emitter();
        if (!parser || !emitter) {
            fprintf(stderr, "streaming not available\n");
            return 3;
        }
        std::string out;
        auto sink = [&out](const std::string &c) -> bool { out += c; return true; };
        auto emit = [&](const StreamEvent &ev) -> bool {
            return emitter->emit(ev, sink);
        };
        parser->feed(body.data(), body.size(), emit);
        parser->finish(emit);
        emitter->finish(sink);
        printf("%s", out.c_str());
        return 0;
    }
    json j = json::parse(body);
    if (is_request) {
        ChatRequest ir;
        if (!fc.parse_request(j, ir, err)) {
            fprintf(stderr, "parse error: %s\n", err.c_str());
            return 1;
        }
        printf("%s\n", tc.serialize_request(ir).dump(2).c_str());
    } else {
        ChatResponse ir;
        if (!fc.parse_response(j, ir, err)) {
            fprintf(stderr, "parse error: %s\n", err.c_str());
            return 1;
        }
        printf("%s\n", tc.serialize_response(ir).dump(2).c_str());
    }
    return 0;
}
