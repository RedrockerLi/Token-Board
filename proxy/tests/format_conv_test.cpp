/// Standalone verification for the three format codecs.
///
///   format_conv_test --self-test          run the embedded round-trip matrix
///   format_conv_test --request --from <f> --to <g> [--stream] < body.json
///                                         convert one request body (f→g)
///   format_conv_test --response --from <f> --to <g> < body.json
///                                         convert one response body (f→g)
///
/// Formats: openai | anthropic | responses | google.  Exit code 0 = all checks passed.
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
#include "format_google.h"
#include "format_media.h"
#include "format_openai.h"
#include "format_responses.h"
#include "reasoning_bridge.h"

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

// The model and the stream flag live in the URL on this wire, so the sample
// intentionally carries neither: the HTTP layer supplies them.
static const char *kGoogleRequest = R"({
  "systemInstruction": {"parts": [{"text": "You are helpful."}]},
  "contents": [
    {"role": "user", "parts": [{"text": "Hi"}]},
    {"role": "model", "parts": [
      {"functionCall": {"id": "call_1", "name": "get_weather", "args": {"city": "SF"}},
       "thoughtSignature": "c2ln"}]},
    {"role": "user", "parts": [
      {"functionResponse": {"id": "call_1", "name": "get_weather",
                            "response": {"name": "get_weather", "content": "72F"}}}]}
  ],
  "tools": [{"functionDeclarations": [{"name": "get_weather", "description": "Get weather",
    "parameters": {"type": "OBJECT", "properties": {"city": {"type": "STRING"}},
                   "required": ["city"]}}]}],
  "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
  "generationConfig": {"temperature": 0.5, "maxOutputTokens": 100, "topP": 0.9,
    "thinkingConfig": {"thinkingBudget": 1024, "includeThoughts": true}}
})";

static const char *kGoogleResponse = R"({
  "candidates": [{
    "content": {"role": "model", "parts": [
      {"text": "Let me check.", "thoughtSignature": "c2ln"},
      {"functionCall": {"id": "call_1", "name": "get_weather", "args": {"city": "SF"}}}
    ]},
    "finishReason": "STOP",
    "index": 0
  }],
  "usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 20,
                    "candidatesTokenCount": 10, "totalTokenCount": 110},
  "modelVersion": "gemini-2.5-pro-002",
  "responseId": "resp-1"
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

static void test_sse_multiline_data() {
    printf("--- SSE multiline data folding ---\n");
    std::string event_name;
    std::string data;
    const bool parsed = fmt::parse_sse_frame(
        "event: message\ndata: first line\ndata: second line\n\n",
        &event_name, &data);
    check(parsed, "SSE multiline: frame parses");
    check(event_name == "message", "SSE multiline: event name preserved");
    check(data == "first line\nsecond line",
          "SSE multiline: data fields join with LF");
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
        printf("  converted: %s [key fields checked below]\n", label.c_str());
        if (to == ApiFormat::Google) {
            // Gemini keeps the model in the URL path, never in the body; the
            // HTTP layer restores it.  Assert the body stays model-free.
            check(!converted.contains("model"),
                  "  model kept out of the Gemini body");
        } else {
            check(b.model == a.model, "  model preserved");
        }
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

// Terminal recognition is a parser property (the error-path classifiers read
// the parser's MessageFinish events), not an emitter-shape property: every
// emitter synthesizes its terminator at finish() regardless.  Count the
// parser's own MessageFinish publications.
static int count_message_finish(const CodecRegistry &reg, ApiFormat from,
                                const std::string &sse) {
    const FormatCodec &fc = reg.get(from);
    auto parser = fc.make_stream_parser();
    if (!parser) return -1;
    int finishes = 0;
    auto emit = [&finishes](const StreamEvent &ev) -> bool {
        if (ev.type == StreamEventType::MessageFinish) finishes++;
        return true;
    };
    parser->feed(sse.data(), sse.size(), emit);
    parser->finish(emit);
    return finishes;
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

static int count_substr(const std::string &s, const std::string &needle) {
    int n = 0;
    for (size_t pos = 0; (pos = s.find(needle, pos)) != std::string::npos;
         pos += needle.size())
        n++;
    return n;
}

// ── Stream terminal-event recognition ────────────────────────────────────
// Providers may end an Anthropic stream with only message_stop (no
// stop_reason-bearing message_delta) and an OpenAI stream with only [DONE]
// (no finish_reason chunk).  Those are complete streams; the conversions
// must still produce a terminal frame WITHOUT inventing a stop reason the
// upstream never stated (the reason is passed through as absent/null), and a
// real stop_reason seen earlier must not be overwritten by the terminator.

static void test_anthropic_message_stop_only(const CodecRegistry &reg) {
    printf("--- Anthropic message_stop-only terminal ---\n");
    std::string sse =
        "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"mm\"}}\n\n"
        "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
        "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n"
        "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    const std::string openai =
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAI, sse);
    check(count_substr(openai, "\"finish_reason\":\"stop\"") == 0,
          "message_stop-only: no invented finish_reason");
    check(count_substr(openai, "\"finish_reason\":\"") == 0,
          "message_stop-only: no finish_reason chunk at all");
    check(openai.find("data: [DONE]") != std::string::npos,
          "message_stop-only: stream ends with [DONE]");
    const std::string responses =
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAIResponses, sse);
    check(responses.find("response.completed") != std::string::npos,
          "message_stop-only: Responses target completes");
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::Anthropic, sse));
    check(!frames.empty() && frames.back().first == "message_stop",
          "message_stop-only: Anthropic target ends with message_stop");
    check(count_frames(frames, "message_delta") == 0,
          "message_stop-only: no reason-less message_delta is emitted");
    check(count_message_finish(reg, ApiFormat::Anthropic, sse) == 1,
          "message_stop-only: parser publishes exactly one terminal");
}

static void test_anthropic_stop_reason_survives_message_stop(
    const CodecRegistry &reg) {
    printf("--- Anthropic stop_reason survives trailing message_stop ---\n");
    std::string sse =
        "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"mm\"}}\n\n"
        "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
        "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"x\"}}\n\n"
        "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"max_tokens\",\"stop_sequence\":null}}\n\n"
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    const std::string openai =
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAI, sse);
    check(count_substr(openai, "\"finish_reason\":\"length\"") == 1,
          "max_tokens: finish_reason=length preserved");
    check(count_substr(openai, "\"finish_reason\":\"stop\"") == 0,
          "max_tokens: no downgraded stop frame");
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::Anthropic, sse));
    check(count_frames(frames, "message_delta") == 1,
          "max_tokens: exactly one message_delta");
    bool preserved = false;
    for (const auto &f : frames)
        if (f.first == "message_delta" &&
            f.second.value("delta", json()).value("stop_reason", "") == "max_tokens")
            preserved = true;
    check(preserved, "max_tokens: stop_reason preserved in message_delta");
    check(count_message_finish(reg, ApiFormat::Anthropic, sse) == 1,
          "max_tokens: trailing message_stop does not re-publish the terminal");
}

static void test_anthropic_tool_use_survives_message_stop(
    const CodecRegistry &reg) {
    printf("--- Anthropic tool_use survives trailing message_stop ---\n");
    std::string sse =
        "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"mm\"}}\n\n"
        "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"c1\",\"name\":\"f\",\"input\":{}}}\n\n"
        "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
        "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\",\"stop_sequence\":null}}\n\n"
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    const std::string openai =
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAI, sse);
    check(count_substr(openai, "\"finish_reason\":\"tool_calls\"") == 1,
          "tool_use: finish_reason=tool_calls preserved");
    check(count_substr(openai, "\"finish_reason\":\"stop\"") == 0,
          "tool_use: no downgraded stop frame");
    check(count_message_finish(reg, ApiFormat::Anthropic, sse) == 1,
          "tool_use: parser publishes exactly one terminal");
}

static void test_openai_done_only_terminal(const CodecRegistry &reg) {
    printf("--- OpenAI [DONE]-only terminal ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}]}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    check(!frames.empty() && frames.back().first == "message_stop",
          "[DONE]-only: last frame is message_stop");
    check(count_frames(frames, "message_delta") == 0,
          "[DONE]-only: no reason-less message_delta is emitted");
    check(count_message_finish(reg, ApiFormat::OpenAI, sse) == 1,
          "[DONE]-only: parser publishes exactly one terminal");
}

static void test_openai_finish_then_done_dedupe(const CodecRegistry &reg) {
    printf("--- OpenAI finish_reason + [DONE] dedupe ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"type\":\"function\",\"function\":{\"name\":\"f\",\"arguments\":\"{}\"}}]},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    check(count_frames(frames, "message_delta") == 1,
          "finish+[DONE]: exactly one message_delta");
    bool tool_use = false;
    for (const auto &f : frames)
        if (f.first == "message_delta" &&
            f.second.value("delta", json()).value("stop_reason", "") == "tool_use")
            tool_use = true;
    check(tool_use, "finish+[DONE]: tool_calls not downgraded by the sentinel");
    size_t stop_idx = frames.size(), delta_idx = frames.size();
    for (size_t i = 0; i < frames.size(); i++) {
        if (frames[i].first == "content_block_stop" && stop_idx == frames.size())
            stop_idx = i;
        if (frames[i].first == "message_delta" && delta_idx == frames.size())
            delta_idx = i;
    }
    check(stop_idx < delta_idx,
          "finish+[DONE]: message_delta follows content_block_stop");
    check(count_message_finish(reg, ApiFormat::OpenAI, sse) == 1,
          "finish+[DONE]: trailing sentinel does not re-publish the terminal");
}

static void test_terminal_frame_without_blank_line(const CodecRegistry &reg) {
    printf("--- terminal frame without trailing blank line ---\n");
    // The buffered tail is only dispatched by parser->finish(); the terminal
    // event must still be produced.
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}]}\n\n"
        "data: [DONE]";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    check(!frames.empty() && frames.back().first == "message_stop",
          "no-blank-line: terminal still produced");
    check(count_message_finish(reg, ApiFormat::OpenAI, sse) == 1,
          "no-blank-line: EOF flush publishes the terminal");
}

static void test_responses_terminal_without_envelope(const CodecRegistry &reg) {
    printf("--- Responses terminal without response envelope ---\n");
    std::string sse =
        "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n"
        "event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAIResponses, ApiFormat::Anthropic, sse));
    check(!frames.empty() && frames.back().first == "message_stop",
          "responses no-envelope: terminal still produced");
    check(count_message_finish(reg, ApiFormat::OpenAIResponses, sse) == 1,
          "responses no-envelope: parser publishes exactly one terminal");
    // The SSE event name alone must also terminate (payload without a type).
    std::string named_only =
        "event: response.output_text.delta\ndata: {\"delta\":\"hi\"}\n\n"
        "event: response.completed\ndata: {}\n\n";
    check(count_message_finish(reg, ApiFormat::OpenAIResponses, named_only) == 1,
          "responses no-envelope: event name alone publishes the terminal");
}

// A tool-call stream terminated by the sentinel alone (no finish_reason):
// the reason stays absent end-to-end — never an invented tool_calls, and
// never a downgraded stop.
static void test_openai_done_only_tool_terminal(const CodecRegistry &reg) {
    printf("--- OpenAI [DONE]-only tool terminal ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"type\":\"function\",\"function\":{\"name\":\"f\",\"arguments\":\"{}\"}}]},\"finish_reason\":null}]}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::Anthropic, sse));
    check(count_frames(frames, "message_delta") == 0,
          "[DONE]-only tool: reason stays absent (no message_delta)");
    check(!frames.empty() && frames.back().first == "message_stop",
          "[DONE]-only tool: stream still terminates with message_stop");
    const std::string openai =
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::OpenAI, sse);
    check(count_substr(openai, "\"finish_reason\":\"tool_calls\"") == 0,
          "[DONE]-only tool: no invented finish_reason=tool_calls");
    check(count_substr(openai, "\"finish_reason\":\"") == 0,
          "[DONE]-only tool: no finish_reason chunk at all");
    check(openai.find("data: [DONE]") != std::string::npos,
          "[DONE]-only tool: stream ends with [DONE]");
}

// The opencode.ai gateway ends an OpenAI-compatible stream with an
// `x-opencode-type:"inference-cost"` frame instead of a standard `usage`
// object; its buckets must reach the UsageEvent with the cache read folded
// back into prompt and the cache writes kept out of it.
static void test_openai_stream_opencode_usage_frame(const CodecRegistry &reg) {
    printf("--- OpenAI stream opencode inference-cost usage frame ---\n");
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    auto decode = [&oc](const std::string &sse) {
        auto parser = oc.make_stream_parser();
        std::vector<Usage> usages;
        auto emit = [&usages](const StreamEvent &ev) -> bool {
            if (ev.type == StreamEventType::UsageEvent)
                usages.push_back(ev.usage);
            return true;
        };
        parser->feed(sse.data(), sse.size(), emit);
        parser->finish(emit);
        return usages;
    };

    // The frame the real gateway (and the mock upstream) sends: input 11 with
    // no cache traffic → prompt 11, completion 7, total 18.
    auto usages = decode(
        "data: {\"choices\":[],\"x-opencode-type\":\"inference-cost\","
        "\"cost\":\"0.00000123\",\"normalizedUsage\":{\"inputTokens\":11,"
        "\"outputTokens\":7,\"reasoningTokens\":5,\"cacheReadTokens\":0,"
        "\"cacheWrite5mTokens\":0,\"cacheWrite1hTokens\":0}}\n\n"
        "data: [DONE]\n\n");
    check(usages.size() == 1,
          "opencode usage frame: exactly one UsageEvent");
    if (usages.size() == 1) {
        check(usages[0].prompt_tokens == 11 &&
                  usages[0].completion_tokens == 7 &&
                  usages[0].cache_read_tokens == 0 &&
                  usages[0].cache_creation_tokens == 0 &&
                  usages[0].total_tokens == 18,
              "opencode usage frame: prompt 11 / completion 7 / total 18");
    }

    // Cache traffic: inputTokens excludes the cache read (fold it back in)
    // while the write buckets stay inside it (do not add them to prompt).
    usages = decode(
        "data: {\"choices\":[],\"x-opencode-type\":\"inference-cost\","
        "\"cost\":\"0.00000123\",\"normalizedUsage\":{\"inputTokens\":11,"
        "\"outputTokens\":7,\"cacheReadTokens\":25,"
        "\"cacheWrite5mTokens\":4,\"cacheWrite1hTokens\":2}}\n\n"
        "data: [DONE]\n\n");
    check(usages.size() == 1,
          "opencode usage frame cache: exactly one UsageEvent");
    if (usages.size() == 1) {
        check(usages[0].prompt_tokens == 36 &&
                  usages[0].completion_tokens == 7 &&
                  usages[0].cache_read_tokens == 25 &&
                  usages[0].cache_creation_tokens == 6 &&
                  usages[0].total_tokens == 43,
              "opencode usage frame cache: prompt 36 / read 25 / creation 6 / total 43");
    }

    // A frame carrying both shapes: the inference-cost frame wins and only one
    // UsageEvent leaves the decoder.
    usages = decode(
        "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":1,"
        "\"completion_tokens\":1,\"total_tokens\":2},"
        "\"x-opencode-type\":\"inference-cost\",\"normalizedUsage\":"
        "{\"inputTokens\":11,\"outputTokens\":7}}\n\n"
        "data: [DONE]\n\n");
    check(usages.size() == 1,
          "opencode usage frame priority: exactly one UsageEvent");
    if (usages.size() == 1)
        check(usages[0].prompt_tokens == 11 &&
                  usages[0].completion_tokens == 7 &&
                  usages[0].total_tokens == 18,
              "opencode usage frame priority: frame beats the usage object");

    // Malformed counts read as absent, never as negative or partial values.
    usages = decode(
        "data: {\"choices\":[],\"x-opencode-type\":\"inference-cost\","
        "\"normalizedUsage\":{\"inputTokens\":-3,\"outputTokens\":1.5,"
        "\"cacheReadTokens\":\"x\",\"cacheWrite5mTokens\":null}}\n\n"
        "data: [DONE]\n\n");
    check(usages.size() == 1,
          "opencode usage frame malformed: exactly one UsageEvent");
    if (usages.size() == 1)
        check(usages[0].prompt_tokens == 0 &&
                  usages[0].completion_tokens == 0 &&
                  usages[0].cache_read_tokens == 0 &&
                  usages[0].cache_creation_tokens == 0 &&
                  usages[0].total_tokens == 0,
              "opencode usage frame malformed: bad counts read as 0");

    // A normalizedUsage that names no bucket field says nothing about tokens:
    // it must not displace an accompanying standard usage object, and on its
    // own it yields no UsageEvent at all.
    usages = decode(
        "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":500,"
        "\"completion_tokens\":100,\"total_tokens\":600},"
        "\"x-opencode-type\":\"inference-cost\",\"normalizedUsage\":{}}\n\n"
        "data: [DONE]\n\n");
    check(usages.size() == 1,
          "opencode usage frame empty: exactly one UsageEvent");
    if (usages.size() == 1)
        check(usages[0].prompt_tokens == 500 &&
                  usages[0].completion_tokens == 100 &&
                  usages[0].total_tokens == 600,
              "opencode usage frame empty: usage object stays authoritative");
    usages = decode(
        "data: {\"choices\":[],\"x-opencode-type\":\"inference-cost\","
        "\"normalizedUsage\":{}}\n\n"
        "data: [DONE]\n\n");
    check(usages.empty(),
          "opencode usage frame empty: no UsageEvent without any bucket");
}

// The reverse shape: an Anthropic tool stream whose only terminal is
// message_stop (message_delta carries usage, stop_reason null).  The absent
// reason stays absent — no invented tool_calls, no downgraded stop.
static void test_anthropic_tool_message_stop_only(const CodecRegistry &reg) {
    printf("--- Anthropic tool stream, message_stop-only terminal ---\n");
    std::string sse =
        "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"mm\"}}\n\n"
        "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"c1\",\"name\":\"f\",\"input\":{}}}\n\n"
        "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n"
        "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":null,\"stop_sequence\":null},\"usage\":{\"output_tokens\":3}}\n\n"
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    const std::string openai =
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAI, sse);
    check(count_substr(openai, "\"finish_reason\":\"tool_calls\"") == 0,
          "message_stop-only tool: no invented finish_reason=tool_calls");
    check(count_substr(openai, "\"finish_reason\":\"stop\"") == 0,
          "message_stop-only tool: no downgraded stop frame");
    check(openai.find("data: [DONE]") != std::string::npos,
          "message_stop-only tool: stream still ends with [DONE]");
}

// The Anthropic wire states input_tokens as the mutually exclusive net input:
// cache reads/writes live in their own buckets and are not part of it.  An IR
// prompt parsed from an OpenAI-family wire already includes the cache traffic,
// so the Anthropic serializer (message_start and the non-streaming body) must
// subtract it back out; a native Anthropic conversion and a context-less call
// keep the raw value.
static void test_anthropic_target_usage_mirror(const CodecRegistry &reg) {
    printf("--- Anthropic target usage mirror ---\n");
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    ChatResponse ir;
    std::string err;
    if (!oc.parse_response(json::parse(kOpenaiResponse), ir, err)) {
        check(false, "usage mirror: parse openai fixture: " + err);
        return;
    }
    check(ir.usage.prompt_tokens == 11 && ir.usage.cache_read_tokens == 3 &&
              ir.usage.completion_tokens == 7,
          "usage mirror: OpenAI fixture IR is prompt 11 / read 3 / completion 7");

    ConversionContext from_openai;
    from_openai.source = ApiFormat::OpenAI;
    from_openai.target = ApiFormat::Anthropic;
    const json converted_usage =
        ac.serialize_response(ir, &from_openai).value("usage", json());
    check(converted_usage.value("input_tokens", -1) == 8,
          "usage mirror: OpenAI source → exclusive input_tokens 8");
    check(converted_usage.value("cache_read_input_tokens", -1) == 3 &&
              converted_usage.value("cache_creation_input_tokens", -1) == 0 &&
              converted_usage.value("output_tokens", -1) == 7,
          "usage mirror: cache buckets and output stay untouched");

    check(ac.serialize_response(ir).value("usage", json())
              .value("input_tokens", -1) == 11,
          "usage mirror: no context keeps the raw input_tokens 11");

    ConversionContext from_anthropic;
    from_anthropic.source = ApiFormat::Anthropic;
    from_anthropic.target = ApiFormat::Anthropic;
    check(ac.serialize_response(ir, &from_anthropic).value("usage", json())
              .value("input_tokens", -1) == 11,
          "usage mirror: Anthropic source keeps the raw input_tokens 11");

    auto message_start_usage = [&](const ConversionContext *context) {
        auto emitter = ac.make_stream_emitter(context);
        std::string out;
        auto sink = [&out](const std::string &chunk) -> bool {
            out += chunk;
            return true;
        };
        StreamEvent ev;
        ev.type = StreamEventType::MessageStart;
        ev.usage = ir.usage;
        ev.extra["id"] = "msg-mirror";
        ev.extra["model"] = "mm";
        emitter->emit(ev, sink);
        emitter->finish(sink);
        for (const auto &frame : split_anthropic_frames(out))
            if (frame.first == "message_start")
                return frame.second.value("message", json()).value("usage", json());
        return json();
    };
    const json start_usage = message_start_usage(&from_openai);
    check(start_usage.value("input_tokens", -1) == 8 &&
              start_usage.value("cache_read_input_tokens", -1) == 3 &&
              start_usage.value("output_tokens", -1) == 7,
          "usage mirror: OpenAI source message_start input_tokens 8");
    check(message_start_usage(nullptr).value("input_tokens", -1) == 11,
          "usage mirror: context-less message_start keeps input_tokens 11");
}

// OpenAI-family clients receive the cache-inclusive input view: an IR prompt
// from the Anthropic wire (mutually exclusive net input) is folded together
// with both cache buckets and the total is recomputed, mirroring the stored
// row; a context-less call keeps the IR values verbatim.
static void test_openai_target_usage_fold(const CodecRegistry &reg) {
    printf("--- OpenAI/Responses target usage fold ---\n");
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    const FormatCodec &ac = reg.get(ApiFormat::Anthropic);
    const FormatCodec &rc = reg.get(ApiFormat::OpenAIResponses);
    ChatResponse ir;
    std::string err;
    if (!ac.parse_response(json::parse(kAnthropicResponse), ir, err)) {
        check(false, "usage fold: parse anthropic fixture: " + err);
        return;
    }
    check(ir.usage.prompt_tokens == 11 && ir.usage.cache_read_tokens == 3 &&
              ir.usage.cache_creation_tokens == 2 &&
              ir.usage.completion_tokens == 7,
          "usage fold: Anthropic fixture IR is prompt 11 / read 3 / creation 2 / completion 7");

    ConversionContext from_anthropic;
    from_anthropic.source = ApiFormat::Anthropic;
    from_anthropic.target = ApiFormat::OpenAI;
    const json openai_usage =
        oc.serialize_response(ir, &from_anthropic).value("usage", json());
    check(openai_usage.value("prompt_tokens", -1) == 16 &&
              openai_usage.value("completion_tokens", -1) == 7 &&
              openai_usage.value("total_tokens", -1) == 23,
          "usage fold: OpenAI target prompt 16 / total 23");
    check(openai_usage.value("prompt_tokens_details", json::object())
              .value("cached_tokens", -1) == 3,
          "usage fold: OpenAI target cached_tokens 3");
    check(oc.serialize_response(ir).value("usage", json())
              .value("prompt_tokens", -1) == 11 &&
              oc.serialize_response(ir).value("usage", json())
              .value("total_tokens", -1) == 18,
          "usage fold: no context keeps the raw prompt 11 / total 18");

    ConversionContext to_responses = from_anthropic;
    to_responses.target = ApiFormat::OpenAIResponses;
    const json responses_usage =
        rc.serialize_response(ir, &to_responses).value("usage", json());
    check(responses_usage.value("input_tokens", -1) == 16 &&
              responses_usage.value("output_tokens", -1) == 7 &&
              responses_usage.value("total_tokens", -1) == 23,
          "usage fold: Responses target input 16 / total 23");
    check(responses_usage.value("input_tokens_details", json::object())
              .value("cached_tokens", -1) == 3,
          "usage fold: Responses target cached_tokens 3");
    check(rc.serialize_response(ir).value("usage", json())
              .value("input_tokens", -1) == 11,
          "usage fold: no context keeps the raw Responses input 11");

    // Streaming: the OpenAI usage chunk carries the same folded view.
    auto usage_chunk_of = [](const std::string &out) {
        json found;
        size_t pos = 0;
        while ((pos = out.find("data: ", pos)) != std::string::npos) {
            const size_t end = out.find('\n', pos);
            const std::string payload = out.substr(
                pos + 6, end == std::string::npos ? end : end - pos - 6);
            pos = end == std::string::npos ? out.size() : end;
            json j = json::parse(payload, nullptr, false);
            if (j.is_object() && j.contains("usage") &&
                j.value("choices", json()) == json::array())
                found = j["usage"];
        }
        return found;
    };
    auto emit_usage_chunk = [&](const ConversionContext *context) {
        auto emitter = oc.make_stream_emitter(context);
        std::string out;
        auto sink = [&out](const std::string &chunk) -> bool {
            out += chunk;
            return true;
        };
        StreamEvent ev;
        ev.type = StreamEventType::UsageEvent;
        ev.usage = ir.usage;
        emitter->emit(ev, sink);
        emitter->finish(sink);
        return usage_chunk_of(out);
    };
    const json chunk = emit_usage_chunk(&from_anthropic);
    check(chunk.value("prompt_tokens", -1) == 16 &&
              chunk.value("completion_tokens", -1) == 7 &&
              chunk.value("total_tokens", -1) == 23,
          "usage fold: OpenAI stream chunk prompt 16 / total 23");
    check(chunk.value("prompt_tokens_details", json::object())
              .value("cached_tokens", -1) == 3,
          "usage fold: OpenAI stream chunk cached_tokens 3");
    check(emit_usage_chunk(nullptr).value("prompt_tokens", -1) == 11,
          "usage fold: context-less stream chunk keeps the raw prompt 11");
}

// Usage arriving before the terminal must not emit response.completed early;
// the completion frame closes the stream after the item frames.
static void test_responses_completed_after_items(const CodecRegistry &reg) {
    printf("--- Responses completion is emitted last ---\n");
    std::string sse =
        "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"m1\",\"model\":\"mm\"}}\n\n"
        "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
        "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n"
        "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":null,\"stop_sequence\":null},\"usage\":{\"output_tokens\":3}}\n\n"
        "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::Anthropic, ApiFormat::OpenAIResponses, sse));
    check(count_frames(frames, "response.completed") == 1,
          "completed-last: exactly one response.completed");
    check(!frames.empty() && frames.back().first == "response.completed",
          "completed-last: response.completed is the final frame");
}

// A tool stream must close its item exactly once.
static void test_responses_tool_done_once(const CodecRegistry &reg) {
    printf("--- Responses tool item is closed once ---\n");
    std::string sse =
        "data: {\"id\":\"1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"c1\",\"type\":\"function\",\"function\":{\"name\":\"f\",\"arguments\":\"{}\"}}]},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"tool_calls\"}]}\n\n"
        "data: [DONE]\n\n";
    auto frames = split_anthropic_frames(
        convert_stream(reg, ApiFormat::OpenAI, ApiFormat::OpenAIResponses, sse));
    check(count_frames(frames, "response.output_item.done") == 1,
          "tool done once: exactly one output_item.done");
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

static void test_responses_optional_message_and_tool_choice(const CodecRegistry &reg) {
    printf("--- Responses optional message discriminator ---\n");
    const FormatCodec &rc = reg.get(ApiFormat::OpenAIResponses);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    ir::ChatRequest req;
    std::string err;
    const json input = json{
        {"model", "gpt-test"},
        {"input", json::array({json{{"role", "user"},
                                     {"content", "hello"}}})},
        {"tool_choice", json{{"type", "function"}, {"name", "lookup"}}},
        {"previous_response_id", "resp_prev"},
        {"background", true},
        {"top_p", 0.9},
    };
    check(rc.parse_request(input, req, err),
          "Responses optional message: parse succeeds");
    check(req.messages.size() == 1 && req.messages[0].role == "user" &&
              req.messages[0].content.size() == 1 &&
              req.messages[0].content[0].text == "hello",
          "Responses optional message: user content is retained");
    const json out = oc.serialize_request(req);
    check(out.contains("messages") && out["messages"].size() == 1 &&
              out["messages"][0].value("content", "") == "hello",
          "Responses optional message: converted Chat message is present");
    check(out.value("tool_choice", json()) ==
              json{{"type", "function"}, {"function", {{"name", "lookup"}}}},
          "Responses tool_choice: converted to Chat function shape");
    const json roundtrip = rc.serialize_request(req);
    check(roundtrip.value("previous_response_id", "") == "resp_prev" &&
              roundtrip.value("background", false) &&
              roundtrip.value("top_p", 0.0) == 0.9,
          "Responses known fields: preserved by Responses serialization");
    ir::ChatRequest rich;
    const json rich_input = json{
        {"model", "gpt-test"},
        {"input", json::array({json{{"role", "user"}, {"content", json::array({
            json{{"type", "input_file"}, {"file_id", "file_1"}},
            json{{"type", "input_audio"}, {"data", "AQI="}, {"format", "wav"}},
        })}}})},
    };
    check(rc.parse_request(rich_input, rich, err),
          "Responses file/audio parts: parse succeeds");
    const json rich_roundtrip = rc.serialize_request(rich);
    check(rich_roundtrip["input"][0]["content"][0].value("type", "") == "input_file" &&
              rich_roundtrip["input"][0]["content"][1].value("type", "") == "input_audio",
          "Responses file/audio parts: native parts preserved");
}

static void test_responses_custom_tool_items(const CodecRegistry &reg) {
    printf("--- Responses custom tool items ---\n");
    const FormatCodec &rc = reg.get(ApiFormat::OpenAIResponses);
    const FormatCodec &oc = reg.get(ApiFormat::OpenAI);
    ir::ChatRequest req;
    std::string err;
    const json input = json{
        {"model", "gpt-test"},
        {"input", json::array({
            json{{"type", "custom_tool_call"}, {"call_id", "call_1"},
                 {"name", "apply_patch"}, {"input", "*** patch ***"}},
            json{{"type", "custom_tool_call_output"}, {"call_id", "call_1"},
                 {"output", "ok"}},
        })},
        {"tools", json::array({json{{"type", "custom"},
                                      {"name", "apply_patch"},
                                      {"description", "patch files"},
                                      {"format", json{{"type", "grammar"},
                                                       {"syntax", "lark"}}}}})},
    };
    check(rc.parse_request(input, req, err), "Responses custom tool: parse succeeds");
    const json out = oc.serialize_request(req);
    bool tool_call = false, tool_result = false;
    for (const auto &m : out.value("messages", json::array())) {
        if (m.value("role", "") == "assistant" && m.contains("tool_calls"))
            tool_call = true;
        if (m.value("role", "") == "tool" &&
            m.value("tool_call_id", "") == "call_1" &&
            m.value("content", "") == "ok")
            tool_result = true;
    }
    check(tool_call, "Responses custom tool: assistant tool call retained");
    check(tool_result, "Responses custom tool: tool output retained");
    check(!out.value("tools", json::array()).empty() &&
              out["tools"][0].value("type", "") == "function",
          "Responses custom tool: Chat function adapter emitted");
    const json same = rc.serialize_request(req);
    check(same["tools"][0].value("format", json()) ==
              json{{"type", "grammar"}, {"syntax", "lark"}},
          "Responses custom tool: raw format preserved");
    check(same["input"][0].value("type", "") == "custom_tool_call" &&
              same["input"][0].value("input", "") == "*** patch ***" &&
              same["input"][1].value("type", "") == "custom_tool_call_output",
          "Responses custom tool: call/output item types preserved");
    ir::ChatResponse response;
    const json response_input = json{
        {"id", "resp-custom"}, {"model", "gpt-test"}, {"status", "completed"},
        {"output", json::array({json{{"id", "cc_1"},
            {"type", "custom_tool_call"}, {"call_id", "call_1"},
            {"name", "apply_patch"}, {"input", "*** patch ***"}}})},
    };
    check(rc.parse_response(response_input, response, err),
          "Responses custom tool response: parse succeeds");
    const json response_roundtrip = rc.serialize_response(response);
    check(response_roundtrip["output"][0].value("type", "") == "custom_tool_call" &&
              response_roundtrip["output"][0].value("input", "") == "*** patch ***",
          "Responses custom tool response: native item preserved");
}

static void test_responses_stream_lifecycle(const CodecRegistry &reg) {
    printf("--- Responses stream lifecycle ---\n");
    const std::string openai_sse =
        "data: {\"id\":\"chat-1\",\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"Think\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"Hi\"},\"finish_reason\":null}]}\n\n"
        "data: {\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":1,\"total_tokens\":3}}\n\n"
        "data: [DONE]\n\n";
    const std::string out = convert_stream(reg, ApiFormat::OpenAI,
                                           ApiFormat::OpenAIResponses,
                                           openai_sse);
    check(out.find("event: response.created\n") != std::string::npos,
          "Responses stream: response.created event line");
    check(out.find("event: response.in_progress\n") != std::string::npos,
          "Responses stream: response.in_progress event");
    check(out.find("response.output_text.done") != std::string::npos &&
              out.find("response.content_part.done") != std::string::npos &&
              out.find("response.output_item.done") != std::string::npos,
          "Responses stream: text/content/item done events");
    check(out.find("response.reasoning_summary_part.added") != std::string::npos &&
              out.find("response.reasoning_summary_text.delta") != std::string::npos &&
              out.find("response.reasoning_summary_text.done") != std::string::npos &&
              out.find("response.reasoning_summary_part.done") != std::string::npos,
          "Responses stream: reasoning summary lifecycle");
    check(out.find("event: response.completed\n") != std::string::npos,
          "Responses stream: response.completed event");
}

static void test_native_media_bridges(const CodecRegistry &reg) {
    printf("--- Native multimodal tool-result bridges ---\n");
    const auto &rc = reg.get(ApiFormat::OpenAIResponses);
    const auto &oc = reg.get(ApiFormat::OpenAI);
    const auto &ac = reg.get(ApiFormat::Anthropic);
    std::string err;
    const json responses = {
        {"model", "m"}, {"input", json::array({json{
            {"type", "function_call_output"}, {"call_id", "c1"},
            {"output", json::array({
                json{{"type", "input_text"}, {"text", "ok"}},
                json{{"type", "input_image"}, {"image_url", "data:image/png;base64,IMG_SENTINEL"}},
                json{{"type", "input_file"}, {"file_data", "data:application/pdf;base64,FILE_SENTINEL"}, {"filename", "a.pdf"}},
                json{{"type", "input_audio"}, {"input_audio", {{"data", "AUDIO_SENTINEL"}, {"format", "wav"}}}}
            })}
        }})}
    };
    ChatRequest req;
    check(rc.parse_request(responses, req, err), "media: Responses parse");
    json chat = oc.serialize_request(req);
    bool native_image = false, native_file = false, native_audio = false;
    bool base64_in_tool = false;
    for (const auto &m : chat["messages"]) {
        if (m.value("role", "") == "tool" && m.dump().find("SENTINEL") != std::string::npos)
            base64_in_tool = true;
        if (m.value("role", "") != "user" || !m["content"].is_array()) continue;
        for (const auto &p : m["content"]) {
            native_image = native_image || p.value("type", "") == "image_url";
            native_file = native_file || p.value("type", "") == "file";
            native_audio = native_audio || p.value("type", "") == "input_audio";
        }
    }
    check(native_image && native_file && native_audio,
          "media: Responses→Chat emits native image/file/audio parts");
    check(!base64_in_tool, "media: tool message does not contain media payload");

    ChatRequest anthropic_req;
    const json anthropic = json::parse(R"({
      "model":"m", "max_tokens":10,
      "messages":[
        {"role":"assistant","content":[{"type":"tool_use","id":"c1","name":"x","input":{}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"c1","content":[
          {"type":"image","source":{"type":"base64","media_type":"image/png","data":"IMG_SENTINEL"}}
        ]}]}
      ]
    })");
    check(ac.parse_request(anthropic, anthropic_req, err), "media: Anthropic parse");
    json response_input = rc.serialize_request(anthropic_req);
    check(response_input["input"][1]["output"][0].value("type", "") == "input_image",
          "media: Anthropic→Responses restores input_image");
    json anthropic_output = ac.serialize_request(req);
    check(anthropic_output["messages"].empty() || anthropic_output["messages"][0].value("role", "") != "tool",
          "media: Anthropic tool results use role user");

    const auto requirements = fmt::request_media_requirements(req);
    std::string reason;
    check(!fmt::target_supports_media(ApiFormat::Anthropic, requirements, reason),
          "media: audio rejects Anthropic target");
    check(fmt::target_supports_media(ApiFormat::OpenAI, requirements, reason),
          "media: Chat target accepts image/file/audio");

    std::string huge(17 * 1024, 'A');
    ContentBlock clamped;
    fmt::parse_tool_result_content(json::array({
        json{{"type", "image"}, {"data", "IMG"}, {"mimeType", "image/png"}},
        huge}), clamped);
    check(fmt::tool_result_text(clamped).find("[large media payload omitted]") != std::string::npos,
          "media: residual base64-like text is clamped after media detection");
}

// ── Duplicate message cycle suppression ─────────────────────────────────
// A gateway may splice a retried generation into the same response: a second
// message_start (Anthropic/Responses) or a fresh completion id (OpenAI) after
// the first cycle ended.  The parsers must deliver one cycle and keep the
// first cycle's usage.

struct CycleTally {
    int starts = 0;
    int finishes = 0;
    int text_deltas = 0;
    int completion_tokens = -1;
    bool suppressed = false;
};

static CycleTally tally_cycles(const CodecRegistry &reg, ApiFormat from,
                               const std::string &sse) {
    CycleTally t;
    auto parser = reg.get(from).make_stream_parser();
    if (!parser) return t;
    auto emit = [&t](const StreamEvent &ev) -> bool {
        if (ev.type == StreamEventType::MessageStart) t.starts++;
        if (ev.type == StreamEventType::MessageFinish) t.finishes++;
        if (ev.type == StreamEventType::ContentTextDelta) t.text_deltas++;
        if (ev.type == StreamEventType::UsageEvent &&
            ev.usage.completion_tokens > 0)
            t.completion_tokens = ev.usage.completion_tokens;
        return true;
    };
    parser->feed(sse.data(), sse.size(), emit);
    parser->finish(emit);
    t.suppressed = parser->suppressed_after_terminal();
    return t;
}

static const char *kAnthropicCycle1 =
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":"
    "{\"id\":\"m1\",\"usage\":{\"input_tokens\":11,\"output_tokens\":1}}}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\","
    "\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"first\"}}\n\n"
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":"
    "{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":7}}\n\n"
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

static const char *kAnthropicCycle2 =
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":"
    "{\"id\":\"m2\",\"usage\":{\"input_tokens\":111,\"output_tokens\":1}}}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\","
    "\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"second\"}}\n\n"
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":"
    "{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":777}}\n\n"
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

static const char *kOpenaiCycle1 =
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"role\":\"assistant\"}}]}\n\n"
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"content\":\"first\"}}]}\n\n"
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},"
    "\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":11,"
    "\"completion_tokens\":7,\"total_tokens\":18}}\n\n"
    "data: [DONE]\n\n";

static const char *kOpenaiCycle2 =
    "data: {\"id\":\"c2\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"role\":\"assistant\"}}]}\n\n"
    "data: {\"id\":\"c2\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"content\":\"second\"}}]}\n\n"
    "data: {\"id\":\"c2\",\"choices\":[{\"index\":0,\"delta\":{},"
    "\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":111,"
    "\"completion_tokens\":777,\"total_tokens\":888}}\n\n"
    "data: [DONE]\n\n";

static const char *kResponsesCycle1 =
    "event: response.created\ndata: {\"type\":\"response.created\","
    "\"response\":{\"id\":\"r1\",\"model\":\"m\"}}\n\n"
    "event: response.output_text.delta\ndata: {\"type\":"
    "\"response.output_text.delta\",\"delta\":\"first\",\"item_id\":\"i1\","
    "\"output_index\":0,\"content_index\":0}\n\n"
    "event: response.completed\ndata: {\"type\":\"response.completed\","
    "\"response\":{\"id\":\"r1\",\"status\":\"completed\",\"usage\":"
    "{\"input_tokens\":11,\"output_tokens\":7,\"total_tokens\":18}}}\n\n";

static const char *kResponsesCycle2 =
    "event: response.created\ndata: {\"type\":\"response.created\","
    "\"response\":{\"id\":\"r2\",\"model\":\"m\"}}\n\n"
    "event: response.completed\ndata: {\"type\":\"response.completed\","
    "\"response\":{\"id\":\"r2\",\"status\":\"completed\",\"usage\":"
    "{\"input_tokens\":111,\"output_tokens\":777,\"total_tokens\":888}}}\n\n";

static void test_duplicate_cycle_suppression(const CodecRegistry &reg) {
    {
        const std::string sse =
            std::string(kAnthropicCycle1) + kAnthropicCycle2;
        const CycleTally t = tally_cycles(reg, ApiFormat::Anthropic, sse);
        check(t.starts == 1, "duplicate cycle: anthropic keeps one message_start");
        check(t.finishes == 1, "duplicate cycle: anthropic keeps one terminal");
        check(t.text_deltas == 1, "duplicate cycle: anthropic drops second text");
        check(t.completion_tokens == 7,
              "duplicate cycle: anthropic keeps first usage");
        check(t.suppressed, "duplicate cycle: anthropic reports suppression");

        const CycleTally single =
            tally_cycles(reg, ApiFormat::Anthropic, kAnthropicCycle1);
        check(single.starts == 1 && single.finishes == 1,
              "single cycle: anthropic unchanged");
        check(!single.suppressed,
              "single cycle: trailing message_stop is not suppression");
    }
    {
        const std::string sse = std::string(kOpenaiCycle1) + kOpenaiCycle2;
        const CycleTally t = tally_cycles(reg, ApiFormat::OpenAI, sse);
        check(t.text_deltas == 1,
              "duplicate cycle: openai drops second completion text");
        check(t.completion_tokens == 7,
              "duplicate cycle: openai keeps first usage");
        check(t.suppressed, "duplicate cycle: openai reports suppression");

        const CycleTally single =
            tally_cycles(reg, ApiFormat::OpenAI, kOpenaiCycle1);
        check(!single.suppressed, "single cycle: openai is not suppression");
    }
    {
        const std::string sse =
            std::string(kResponsesCycle1) + kResponsesCycle2;
        const CycleTally t =
            tally_cycles(reg, ApiFormat::OpenAIResponses, sse);
        check(t.starts == 1,
              "duplicate cycle: responses keeps one response.created");
        check(t.finishes == 1,
              "duplicate cycle: responses keeps one terminal");
        check(t.completion_tokens == 7,
              "duplicate cycle: responses keeps first usage");
        check(t.suppressed, "duplicate cycle: responses reports suppression");
    }
}

static void test_google_finish_reason_matrix(const CodecRegistry &reg) {
    printf("--- Google finishReason mapping ---\n");
    struct Case { const char *reason; StopReason want; };
    const Case cases[] = {
        {"STOP", StopReason::Stop},
        {"MAX_TOKENS", StopReason::Length},
        {"CONTINUATION", StopReason::Length},
        {"SAFETY", StopReason::ContentFilter},
        {"RECITATION", StopReason::ContentFilter},
        {"BLOCKLIST", StopReason::ContentFilter},
        {"PROHIBITED_CONTENT", StopReason::ContentFilter},
        {"SPII", StopReason::ContentFilter},
        {"IMAGE_SAFETY", StopReason::ContentFilter},
        {"LANGUAGE", StopReason::Unknown},
        {"MALFORMED_FUNCTION_CALL", StopReason::Unknown},
        {"TOO_MANY_TOOL_CALLS", StopReason::Unknown},
        {"FINISH_REASON_UNSPECIFIED", StopReason::Absent},
    };
    for (const auto &entry : cases) {
        const json body = {{"candidates", json::array({{
            {"content", {{"role", "model"},
                         {"parts", json::array({{{"text", "x"}}})}}},
            {"finishReason", entry.reason}, {"index", 0}}})}};
        ChatResponse out;
        std::string err;
        const bool parsed = reg.get(ApiFormat::Google).parse_response(body, out, err);
        check(parsed, std::string("google finishReason ") + entry.reason + " parses");
        check(out.stop_reason == entry.want,
              std::string("google finishReason ") + entry.reason + " maps");
    }
    // A tool-call response reports STOP but means a tool-use stop.
    const json tool_body = {{"candidates", json::array({{
        {"content", {{"role", "model"}, {"parts", json::array({{
            {"functionCall", {{"id", "c1"}, {"name", "f"}, {"args", json::object()}}}}})}}},
        {"finishReason", "STOP"}}})}};
    ChatResponse tool_out;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_response(tool_body, tool_out, err),
          "google tool-call response parses");
    check(tool_out.stop_reason == StopReason::ToolUse,
          "google STOP with a function call maps to tool use");

    struct Reverse { StopReason reason; const char *want; };
    const Reverse reverse[] = {
        {StopReason::Stop, "STOP"},
        {StopReason::ToolUse, "STOP"},
        {StopReason::Length, "MAX_TOKENS"},
        {StopReason::ContentFilter, "SAFETY"},
    };
    for (const auto &entry : reverse) {
        ChatResponse ir;
        ir.stop_reason = entry.reason;
        ir.content.push_back(ContentBlock{});
        ir.content.back().kind = ContentKind::Text;
        ir.content.back().text = "x";
        const json wire = reg.get(ApiFormat::Google).serialize_response(ir);
        check(wire["candidates"][0].value("finishReason", "") == entry.want,
              std::string("IR stop reason serializes to ") + entry.want);
    }
    ChatResponse absent;
    absent.content.push_back(ContentBlock{});
    absent.content.back().kind = ContentKind::Text;
    absent.content.back().text = "x";
    const json absent_wire = reg.get(ApiFormat::Google).serialize_response(absent);
    check(!absent_wire["candidates"][0].contains("finishReason"),
          "an undefined stop reason is not fabricated");
}

static void test_google_usage_mapping(const CodecRegistry &reg) {
    printf("--- Google usage mapping ---\n");
    const json body = {{"usageMetadata", {
        {"promptTokenCount", 100}, {"cachedContentTokenCount", 60},
        {"candidatesTokenCount", 25}, {"thoughtsTokenCount", 128},
        {"toolUsePromptTokenCount", 10}, {"totalTokenCount", 263}}}};
    ChatResponse ir;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_response(body, ir, err),
          "google usage-only response parses");
    check(ir.usage.prompt_tokens == 110, "prompt includes cache and tool-use tokens");
    check(ir.usage.completion_tokens == 153, "completion includes thinking tokens");
    check(ir.usage.cache_read_tokens == 60, "cache read becomes the cache bucket");
    check(ir.usage.cache_creation_tokens == 0, "this wire has no cache-creation bucket");
    check(ir.usage.total_tokens == 263, "reported total is preserved");
    const json wire = reg.get(ApiFormat::Google).serialize_response(ir);
    check(wire["usageMetadata"].value("promptTokenCount", 0) == 110,
          "serialized prompt stays cache-inclusive");
    check(wire["usageMetadata"].value("candidatesTokenCount", 0) == 25,
          "serialized candidates exclude thinking tokens");
    check(wire["usageMetadata"].value("thoughtsTokenCount", 0) == 128,
          "serialized thoughts are reported separately");
    check(wire["usageMetadata"].value("cachedContentTokenCount", 0) == 60,
          "serialized cache read is a subset of prompt");
}

static void test_google_tool_id_and_result_name(const CodecRegistry &reg) {
    printf("--- Google tool ids and result names ---\n");
    const json response = {{"candidates", json::array({{
        {"content", {{"role", "model"}, {"parts", json::array({{
            {"functionCall", {{"name", "get_weather"},
                              {"args", {{"city", "SF"}}}}}}})}}},
        {"finishReason", "STOP"}}})}};
    ChatResponse ir;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_response(response, ir, err),
          "google call without id parses");
    std::string call_id;
    for (const auto &block : ir.content)
        if (block.kind == ContentKind::ToolUse) call_id = block.tool_call_id;
    check(!call_id.empty(), "a missing functionCall id is synthesized");

    // An OpenAI tool message names the call only by id; Gemini requires a name.
    const json request = {
        {"model", "m"},
        {"messages", json::array({
            {{"role", "user"}, {"content", "hi"}},
            {{"role", "assistant"}, {"content", ""},
             {"tool_calls", json::array({{{"id", call_id}, {"type", "function"},
                {"function", {{"name", "get_weather"},
                              {"arguments", "{\"city\":\"SF\"}"}}}}})}},
            {{"role", "tool"}, {"tool_call_id", call_id}, {"content", "72F"}}})}};
    ChatRequest chat;
    check(reg.get(ApiFormat::OpenAI).parse_request(request, chat, err),
          "openai tool request parses");
    const json wire = reg.get(ApiFormat::Google).serialize_request(chat);
    std::string result_name;
    std::string result_id;
    std::string result_text;
    for (const auto &content : wire["contents"])
        for (const auto &part : content["parts"])
            if (part.contains("functionResponse")) {
                result_name = part["functionResponse"].value("name", "");
                result_id = part["functionResponse"].value("id", "");
                result_text = part["functionResponse"]["response"].value("content", "");
            }
    check(result_name == "get_weather",
          "the tool result is named from the matching call");
    check(result_id == call_id, "the tool result keeps its call id");
    check(result_text == "72F", "the tool result text is carried through");
}

static void test_google_thinking_signature(const CodecRegistry &reg) {
    printf("--- Google thought signatures ---\n");
    ChatRequest request;
    request.model = "gemini-3-pro";
    Message message;
    message.role = "assistant";
    ContentBlock call;
    call.kind = ContentKind::ToolUse;
    call.tool_name = "f";
    call.tool_call_id = "c1";
    call.tool_input = json{{"a", 1}};
    message.content.push_back(call);
    request.messages.push_back(message);
    request.items = request.messages;
    auto signature_of = [&reg](const ChatRequest &in) {
        const json wire = reg.get(ApiFormat::Google).serialize_request(in);
        for (const auto &content : wire["contents"])
            for (const auto &part : content["parts"])
                if (part.contains("functionCall"))
                    return part.value("thoughtSignature", std::string());
        return std::string();
    };
    check(signature_of(request) == "skip_thought_signature_validator",
          "gemini 3 replay fills the documented signature sentinel");

    ChatRequest signed_request = request;
    signed_request.messages[0].content[0].extra["thoughtSignature"] = "c2ln";
    signed_request.items = signed_request.messages;
    check(signature_of(signed_request) == "c2ln",
          "a stored signature is replayed verbatim");

    ChatRequest older = request;
    older.model = "gemini-2.5-pro";
    check(signature_of(older).empty(),
          "gemini 2.5 does not receive a synthetic signature");

    // Thought parts round-trip as thinking blocks, not as answer text.
    const json response = {{"candidates", json::array({{
        {"content", {{"role", "model"}, {"parts", json::array({
            {{"text", "pondering"}, {"thought", true}, {"thoughtSignature", "c2ln"}},
            {{"text", "answer"}}})}}},
        {"finishReason", "STOP"}}})}};
    ChatResponse ir;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_response(response, ir, err),
          "google thinking response parses");
    check(ir.content.size() == 2 && ir.content[0].kind == ContentKind::Thinking &&
              ir.content[0].text == "pondering",
          "thought parts become thinking blocks");
    check(ir.content.size() == 2 && ir.content[1].kind == ContentKind::Text &&
              ir.content[1].text == "answer",
          "visible text stays visible");
    check(ir.content[0].extra.value("thoughtSignature", "") == "c2ln",
          "the per-part signature is retained");
}

static void test_google_blocked_prompt(const CodecRegistry &reg) {
    printf("--- Google prompt blocking ---\n");
    const json blocked = {{"promptFeedback", {{"blockReason", "SAFETY"}}},
                          {"usageMetadata", {{"promptTokenCount", 3}}}};
    ChatResponse ir;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_response(blocked, ir, err),
          "a blocked prompt response parses");
    check(ir.stop_reason == StopReason::ContentFilter,
          "a blocked prompt is a content-filter stop, not an empty answer");
    check(ir.content.empty(), "a blocked prompt carries no content");
    const json wire = reg.get(ApiFormat::Google).serialize_response(ir);
    check(wire["candidates"][0].value("finishReason", "") == "SAFETY",
          "a blocked prompt serializes as SAFETY");
}

static void test_google_error_envelope(const CodecRegistry &reg) {
    printf("--- Google error envelope ---\n");
    const json upstream = {{"error", {
        {"code", 429}, {"message", "quota exceeded"},
        {"status", "RESOURCE_EXHAUSTED"},
        {"details", json::array({{{"@type", "type.googleapis.com/google.rpc.RetryInfo"},
                                  {"retryDelay", "16s"}}})}}}};
    const json normalized = reg.get(ApiFormat::Google).parse_error_body(upstream);
    check(normalized.value("message", "") == "quota exceeded",
          "the upstream message survives");
    check(normalized.value("type", "") == "resource_exhausted",
          "the google status becomes the normalized type");
    check(normalized.value("retry_after_seconds", 0) == 16,
          "RetryInfo becomes a retry hint");
    const json wire = reg.get(ApiFormat::Google).serialize_error_body(
        {{"message", "busy"}, {"type", "rate_limit_error"}, {"code", 429}});
    check(wire["error"].value("status", "") == "RESOURCE_EXHAUSTED",
          "429 serializes to RESOURCE_EXHAUSTED");
    check(wire["error"].value("code", 0) == 8,
          "429 serializes to the matching google.rpc code");
}

static void test_google_media_bridge(const CodecRegistry &reg) {
    printf("--- Google inline media bridge ---\n");
    const json request = {{"contents", json::array({{
        {"role", "user"}, {"parts", json::array({
            {{"text", "what is this"}},
            {{"inlineData", {{"mimeType", "image/png"}, {"data", "aGVsbG8="}}}}})}}})}};
    ChatRequest ir;
    std::string err;
    check(reg.get(ApiFormat::Google).parse_request(request, ir, err),
          "google inline media request parses");
    bool image = false;
    for (const auto &message : ir.messages)
        for (const auto &block : message.content)
            if (block.kind == ContentKind::Image &&
                block.image_data_b64 == "aGVsbG8=" &&
                block.media_type == "image/png")
                image = true;
    check(image, "inlineData becomes an IR image block");
    const json openai = reg.get(ApiFormat::OpenAI).serialize_request(ir);
    bool data_uri = false;
    for (const auto &message : openai["messages"])
        if (message.contains("content") && message["content"].is_array())
            for (const auto &part : message["content"])
                if (part.value("type", "") == "image_url" &&
                    part["image_url"].value("url", "").rfind(
                        "data:image/png;base64,aGVsbG8=", 0) == 0)
                    data_uri = true;
    check(data_uri, "the IR image becomes an OpenAI data URI part");
}

static void test_google_stream_events(const CodecRegistry &reg) {
    printf("--- Google stream parse/emit ---\n");
    const std::string sse =
        "data: {\"responseId\":\"r1\",\"modelVersion\":\"gemini-2.5-pro\","
        "\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"Hel\"}]},"
        "\"index\":0}]}\n\n"
        "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":["
        "{\"text\":\"lo\"},{\"text\":\"why\",\"thought\":true}]},\"index\":0}]}\n\n"
        "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":["
        "{\"functionCall\":{\"name\":\"f\",\"args\":{\"a\":1}}}]},\"index\":0}]}\n\n"
        "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[]},"
        "\"finishReason\":\"STOP\",\"index\":0}],\"usageMetadata\":"
        "{\"promptTokenCount\":5,\"candidatesTokenCount\":2,\"totalTokenCount\":7}}\n\n";

    auto parser = reg.get(ApiFormat::Google).make_stream_parser();
    std::vector<StreamEvent> events;
    auto emit = [&events](const StreamEvent &event) {
        events.push_back(event);
        return true;
    };
    parser->feed(sse.data(), sse.size(), emit);
    parser->finish(emit);
    std::string text;
    std::string thinking;
    int tool_starts = 0;
    bool finished = false;
    bool tool_stop = false;
    int total = 0;
    for (const auto &event : events) {
        if (event.type == StreamEventType::ContentTextDelta) text += event.text;
        else if (event.type == StreamEventType::ContentThinkingDelta) thinking += event.text;
        else if (event.type == StreamEventType::ToolCallStart) tool_starts++;
        else if (event.type == StreamEventType::MessageFinish) {
            finished = true;
            tool_stop = event.stop_reason == StopReason::ToolUse;
        } else if (event.type == StreamEventType::UsageEvent) {
            total = event.usage.total_tokens;
        }
    }
    check(text == "Hello", "stream text deltas concatenate");
    check(thinking == "why", "thought deltas stay separate from text");
    check(tool_starts == 1, "stream emits one tool call");
    check(finished && tool_stop, "stream reports a tool-use stop");
    check(total == 7, "stream reports usage");

    const std::string reemitted =
        convert_stream(reg, ApiFormat::Google, ApiFormat::Google, sse);
    auto reparsed_parser = reg.get(ApiFormat::Google).make_stream_parser();
    std::vector<StreamEvent> reparsed;
    auto collect = [&reparsed](const StreamEvent &event) {
        reparsed.push_back(event);
        return true;
    };
    reparsed_parser->feed(reemitted.data(), reemitted.size(), collect);
    reparsed_parser->finish(collect);
    std::string reparsed_text;
    int reparsed_tools = 0;
    for (const auto &event : reparsed) {
        if (event.type == StreamEventType::ContentTextDelta)
            reparsed_text += event.text;
        else if (event.type == StreamEventType::ToolCallStart) reparsed_tools++;
    }
    check(reparsed_text == "Hello", "google→google stream keeps the text");
    check(reparsed_tools == 1, "google→google stream keeps the tool call");
}

// ── Audit 01 (protocol alignment) regression cases ───────────────────────
// Each case names the audit id it pins and asserts the contract that audit
// section 6 asks for, so it stays red until the matching fix lands.

/// Null-safe string member read: a JSON null (not a missing key) is the easy
/// way to abort the whole suite through nlohmann's value() type_error.
static std::string json_string_at(const json &object, const char *key) {
    if (!object.is_object() || !object.contains(key)) return std::string();
    const json &value = object[key];
    return value.is_string() ? value.get<std::string>() : std::string();
}

/// Null-safe object/number read for the same reason.
static json json_object_at(const json &object, const char *key) {
    if (!object.is_object() || !object.contains(key)) return json::object();
    const json &value = object[key];
    return value.is_object() ? value : json::object();
}

/// Null-safe array member read (a JSON null must not abort the suite).
static json json_array_at(const json &object, const char *key) {
    if (!object.is_object() || !object.contains(key)) return json::array();
    const json &value = object[key];
    return value.is_array() ? value : json::array();
}

/// Text of the first `role:"system"` message, joined when it is a block array.
static std::string first_system_message(const json &body) {
    for (const auto &m : json_array_at(body, "messages")) {
        if (json_string_at(m, "role") != "system") continue;
        const json &content = m["content"];
        if (content.is_string()) return content.get<std::string>();
        if (content.is_array()) {
            std::string joined;
            for (const auto &part : content) joined += part.value("text", "");
            return joined;
        }
    }
    return std::string();
}

/// Thinking budget the Anthropic target derives from one effort tier.
static int anthropic_budget_for_effort(const CodecRegistry &reg,
                                       const std::string &effort) {
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    ir::ChatRequest req;
    std::string err;
    json body = json::parse(R"({"model":"m","stream":false,"max_tokens":10,
        "messages":[{"role":"user","content":"hi"}]})");
    body["reasoning_effort"] = effort;
    if (!openai.parse_request(body, req, err)) return -1;
    const json out = anthropic.serialize_request(req);
    if (!out.contains("thinking") || !out["thinking"].contains("budget_tokens"))
        return -1;
    return out["thinking"]["budget_tokens"].get<int>();
}

// 01-P-01: folding several system blocks into one field must not glue them.
static void test_audit_p01_system_fold_separator(const CodecRegistry &reg) {
    printf("--- 01-P-01 folded system blocks keep a blank line ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    const FormatCodec &responses = reg.get(ApiFormat::OpenAIResponses);
    std::string err;

    // Anthropic source, two-block system array.
    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","max_tokens":10,
            "system":[{"type":"text","text":"AAA"},{"type":"text","text":"BBB"}],
            "messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]})");
        const bool parsed = anthropic.parse_request(body, req, err);
        check(parsed, "P-01: anthropic multi-block system parses");
        if (parsed) {
            check(first_system_message(openai.serialize_request(req)) ==
                      "AAA\n\nBBB",
                  "P-01: OpenAI target separates folded system blocks");
            check(json_string_at(responses.serialize_request(req),
                                 "instructions") == "AAA\n\nBBB",
                  "P-01: Responses target separates folded system blocks");
            check(json_string_at(anthropic.serialize_request(req), "system") ==
                      "AAA\n\nBBB",
                  "P-01: Anthropic target separates folded system blocks");
        }
    }
    // OpenAI source, two system messages.
    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","stream":false,"max_tokens":10,
            "messages":[{"role":"system","content":"AAA"},
                        {"role":"system","content":"BBB"},
                        {"role":"user","content":"hi"}]})");
        const bool parsed = openai.parse_request(body, req, err);
        check(parsed, "P-01: openai two-system request parses");
        if (parsed) {
            check(json_string_at(anthropic.serialize_request(req), "system") ==
                      "AAA\n\nBBB",
                  "P-01: Anthropic target separates OpenAI system messages");
            check(json_string_at(responses.serialize_request(req),
                                 "instructions") == "AAA\n\nBBB",
                  "P-01: Responses target separates OpenAI system messages");
        }
    }
}

// 01-P-02: an explicit Anthropic thinking budget must reach an OpenAI target.
static void test_audit_p02_budget_to_effort(const CodecRegistry &reg) {
    printf("--- 01-P-02 Anthropic budget_tokens reaches reasoning_effort ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    struct Case {
        int budget;
        const char *effort;
    } cases[] = {{32000, "high"}, {8192, "high"}, {2048, "medium"},
                 {1024, "low"}};
    for (const auto &item : cases) {
        ir::ChatRequest req;
        std::string err;
        json body = json::parse(R"({"model":"m","max_tokens":10,
            "messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]})");
        body["thinking"] = json{{"type", "enabled"},
                                {"budget_tokens", item.budget}};
        if (!anthropic.parse_request(body, req, err)) {
            check(false, "P-02: anthropic thinking request parses");
            continue;
        }
        const json out = openai.serialize_request(req);
        check(json_string_at(out, "reasoning_effort") == item.effort,
              std::string("P-02: budget ") + std::to_string(item.budget) +
                  " maps to reasoning_effort " + item.effort);
    }
}

// 01-P-03: the thinking budget must grow with the effort tier.
static void test_audit_p03_effort_budget_ladder(const CodecRegistry &reg) {
    printf("--- 01-P-03 thinking budget grows with effort ---\n");
    const int minimal = anthropic_budget_for_effort(reg, "minimal");
    const int low = anthropic_budget_for_effort(reg, "low");
    const int medium = anthropic_budget_for_effort(reg, "medium");
    const int high = anthropic_budget_for_effort(reg, "high");
    const int xhigh = anthropic_budget_for_effort(reg, "xhigh");
    const int unknown = anthropic_budget_for_effort(reg, "not-a-tier");
    check(minimal > 0 && low > minimal,
          "P-03: the minimal tier buys less thinking than low");
    check(low < medium && medium < high && high < xhigh,
          "P-03: the budget is monotonic across the effort tiers");
    check(unknown == medium,
          "P-03: an unknown tier falls back to the medium budget");
}

// 01-P-04: `developer` / `system` turns must reach the Anthropic system field.
static void test_audit_p04_developer_role_system(const CodecRegistry &reg) {
    printf("--- 01-P-04 developer/system turns fold into system ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const FormatCodec &responses = reg.get(ApiFormat::OpenAIResponses);
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    std::string err;

    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","max_tokens":10,
            "instructions":"base","input":[
            {"type":"message","role":"developer","content":[{"type":"input_text","text":"dev rule"}]},
            {"type":"message","role":"system","content":[{"type":"input_text","text":"sys rule"}]},
            {"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}]})");
        const bool parsed = responses.parse_request(body, req, err);
        check(parsed, "P-04: responses developer/system request parses");
        if (parsed) {
            const json out = anthropic.serialize_request(req);
            check(json_string_at(out, "system") ==
                      "base\n\ndev rule\n\nsys rule",
                  "P-04: developer and system items reach the system field");
            bool leaked = false;
            for (const auto &m : json_array_at(out, "messages")) {
                const std::string role = json_string_at(m, "role");
                if (role == "developer" || role == "system") leaked = true;
            }
            check(!leaked,
                  "P-04: no developer/system role reaches the Anthropic wire");
        }
    }
    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","stream":false,"max_tokens":10,
            "messages":[{"role":"developer","content":"dev rule"},
                        {"role":"user","content":"hi"}]})");
        const bool parsed = openai.parse_request(body, req, err);
        check(parsed, "P-04: openai developer request parses");
        if (parsed)
            check(json_string_at(anthropic.serialize_request(req), "system")
                      .find("dev rule") != std::string::npos,
                  "P-04: an OpenAI developer turn reaches the system field");
    }
}

// 01-P-05: malformed role / tool_result combinations are rejected, not guessed.
static void test_audit_p05_malformed_tool_result(const CodecRegistry &reg) {
    printf("--- 01-P-05 malformed Anthropic role/block combinations ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    std::string err;
    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","max_tokens":10,"messages":[
            {"role":"user","content":[{"type":"text","text":"hi"}]},
            {"role":"assistant","content":[
                {"type":"tool_result","tool_use_id":"t1","content":"r"}]}]})");
        err.clear();
        const bool parsed = anthropic.parse_request(body, req, err);
        check(!parsed, "P-05: tool_result in an assistant message is rejected");
        check(err == "tool_result blocks are only valid in user messages",
              "P-05: the rejection names the tool_result rule");
    }
    {
        ir::ChatRequest req;
        json body = json::parse(R"({"model":"m","max_tokens":10,"messages":[
            {"role":"tool","content":[{"type":"text","text":"r"}]}]})");
        err.clear();
        const bool parsed = anthropic.parse_request(body, req, err);
        check(!parsed, "P-05: an unknown Anthropic role is rejected");
        check(err ==
                  "Anthropic message role must be user or assistant, got \"tool\"",
              "P-05: the rejection names the offending role");
    }
}

// 01-P-07: media from different tool results must not merge into one message.
static void test_audit_p07_tool_media_keeps_id(const CodecRegistry &reg) {
    printf("--- 01-P-07 multimodal tool results keep their tool_call_id ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    ir::ChatRequest req;
    std::string err;
    json body = json::parse(R"({"model":"claude-test","max_tokens":100,
        "stream":false,"messages":[
        {"role":"user","content":[{"type":"text","text":"go"}]},
        {"role":"assistant","content":[
            {"type":"tool_use","id":"tu_1","name":"a","input":{}},
            {"type":"tool_use","id":"tu_2","name":"b","input":{}}]},
        {"role":"user","content":[
            {"type":"tool_result","tool_use_id":"tu_1","content":[
                {"type":"text","text":"r1"},
                {"type":"image","source":{"type":"base64","media_type":"image/png",
                                          "data":"iVBORw0KGgo="}}]},
            {"type":"tool_result","tool_use_id":"tu_2","content":[
                {"type":"text","text":"r2"},
                {"type":"image","source":{"type":"base64","media_type":"image/png",
                                          "data":"iVBORw0KGgo="}}]}]}]})");
    if (!anthropic.parse_request(body, req, err)) {
        check(false, "P-07: anthropic multimodal tool_result parses");
        return;
    }
    const json out = openai.serialize_request(req);
    int media_messages = 0;
    bool names_first = false, names_second = false, mixed = false;
    bool tool_media_leak = false;
    for (const auto &m : json_array_at(out, "messages")) {
        const std::string role = json_string_at(m, "role");
        const json content = json_array_at(m, "content");
        if (!content.is_array()) continue;
        bool has_image = false;
        std::string text;
        for (const auto &part : content) {
            if (part.value("type", "") == "image_url") has_image = true;
            if (part.value("type", "") == "text" && text.empty())
                text = part.value("text", "");
        }
        if (role == "tool" && has_image) tool_media_leak = true;
        if (role != "user" || !has_image) continue;
        media_messages++;
        const bool first = text.find("tu_1") != std::string::npos;
        const bool second = text.find("tu_2") != std::string::npos;
        if (first && second) mixed = true;
        names_first = names_first || first;
        names_second = names_second || second;
    }
    check(media_messages == 2, "P-07: one media message per tool call");
    check(!mixed, "P-07: media from different tool calls is not merged");
    check(names_first && names_second,
          "P-07: each media message names its tool_call_id");
    check(!tool_media_leak, "P-07: role:tool messages stay text-only");
}

// 01-P-08: the output-ceiling field follows the target upstream model.
static void test_audit_p08_max_tokens_field(const CodecRegistry &reg) {
    printf("--- 01-P-08 output-token field follows the upstream model ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    struct Case {
        const char *model;
        bool completion;
    } cases[] = {{"deepseek-v4", false}, {"gpt-4o", false},
                 {"o3-mini", true},     {"openai/gpt-5", true}};
    for (const auto &item : cases) {
        ir::ChatRequest req;
        std::string err;
        json body = json::parse(R"({"model":"x","max_tokens":100,
            "messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]})");
        body["model"] = item.model;
        if (!anthropic.parse_request(body, req, err)) {
            check(false, "P-08: anthropic request parses");
            continue;
        }
        const json out = openai.serialize_request(req);
        const bool has_completion = out.contains("max_completion_tokens");
        const bool has_plain = out.contains("max_tokens");
        check(has_completion == item.completion &&
                  has_plain == !item.completion,
              std::string("P-08: ") + item.model +
                  (item.completion ? " uses max_completion_tokens only"
                                   : " uses max_tokens only"));
    }
}

// 01-P-09: the Anthropic target normalizes the tool schema like OpenAI does.
static void test_audit_p09_anthropic_input_schema(const CodecRegistry &reg) {
    printf("--- 01-P-09 Anthropic input_schema is normalized ---\n");
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    const char *bodies[] = {
        R"({"model":"m","stream":false,"messages":[{"role":"user","content":"hi"}],
            "tools":[{"type":"function","function":{"name":"t","parameters":null}}]})",
        R"({"model":"m","stream":false,"messages":[{"role":"user","content":"hi"}],
            "tools":[{"type":"function","function":{"name":"t","parameters":{}}}]})",
    };
    for (const char *text : bodies) {
        ir::ChatRequest req;
        std::string err;
        if (!openai.parse_request(json::parse(text), req, err)) {
            check(false, "P-09: openai tool request parses");
            continue;
        }
        const json out = anthropic.serialize_request(req);
        if (!out.contains("tools") || !out["tools"].is_array() ||
            out["tools"].empty()) {
            check(false, "P-09: the anthropic target emits the tool");
            continue;
        }
        const json schema = json_object_at(out["tools"][0], "input_schema");
        check(json_string_at(schema, "type") == "object" &&
                  schema.contains("properties") &&
                  schema["properties"].is_object(),
              "P-09: input_schema carries type:object and properties");
    }
}

// 01-P-10: schema normalization recurses into nested objects and array items.
static void test_audit_p10_recursive_normalize(const CodecRegistry &reg) {
    printf("--- 01-P-10 nested tool schemas are normalized ---\n");
    const FormatCodec &openai = reg.get(ApiFormat::OpenAI);
    ir::ChatRequest req;
    std::string err;
    json body = json::parse(R"({"model":"m","stream":false,
        "messages":[{"role":"user","content":"hi"}],
        "tools":[{"type":"function","function":{"name":"t","parameters":{
            "type":"object","properties":{
                "cfg":{"properties":{"x":{"type":"string"}}},
                "list":{"type":"array","items":{"properties":{
                    "y":{"type":"number"}}}}}}}}]})");
    if (!openai.parse_request(body, req, err)) {
        check(false, "P-10: nested-schema tool request parses");
        return;
    }
    const json out = openai.serialize_request(req);
    if (!out.contains("tools") || !out["tools"].is_array() ||
        out["tools"].empty()) {
        check(false, "P-10: the openai target emits the tool");
        return;
    }
    const json params = json_object_at(
        json_object_at(out["tools"][0], "function"), "parameters");
    const json properties = json_object_at(params, "properties");
    check(json_string_at(json_object_at(properties, "cfg"), "type") == "object",
          "P-10: a nested object property gains type:object");
    check(json_string_at(json_object_at(json_object_at(properties, "list"),
                                        "items"),
                         "type") == "object",
          "P-10: an array item object gains type:object");
}

// 01-P-12: content filtering must survive the Responses hop.
static void test_audit_p12_stop_reason_roundtrip(const CodecRegistry &reg) {
    printf("--- 01-P-12 content_filter survives the Responses hop ---\n");
    const FormatCodec &responses = reg.get(ApiFormat::OpenAIResponses);
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    {
        ir::ChatResponse response;
        std::string err;
        json body = json::parse(R"({"id":"r1","object":"response","created_at":1,
            "model":"m","status":"incomplete",
            "incomplete_details":{"reason":"content_filter"},"output":[],
            "usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}})");
        if (!responses.parse_response(body, response, err)) {
            check(false, "P-12: responses incomplete response parses");
        } else {
            check(json_string_at(anthropic.serialize_response(response),
                                 "stop_reason") == "refusal",
                  "P-12: Responses content_filter becomes Anthropic refusal");
        }
    }
    {
        ir::ChatResponse response;
        std::string err;
        json body = json::parse(R"({"id":"msg_1","type":"message",
            "role":"assistant","model":"m",
            "content":[{"type":"text","text":"no"}],"stop_reason":"refusal",
            "stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}})");
        if (!anthropic.parse_response(body, response, err)) {
            check(false, "P-12: anthropic refusal response parses");
        } else {
            const json out = responses.serialize_response(response);
            check(json_string_at(out, "status") == "incomplete",
                  "P-12: Anthropic refusal becomes an incomplete response");
            check(json_string_at(json_object_at(out, "incomplete_details"),
                                 "reason") == "content_filter",
                  "P-12: the Responses reason names content_filter");
        }
    }
}

// 01-P-13: enabled thinking must always carry a budget on the wire.
static void test_audit_p13_thinking_budget_fallback(const CodecRegistry &reg) {
    printf("--- 01-P-13 enabled thinking always carries a budget ---\n");
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    ir::ChatRequest req;
    req.model = "m";
    req.max_tokens = 10;
    req.reasoning.enabled = true;  // no budget_tokens and no effort
    ir::Message user;
    user.role = "user";
    ir::ContentBlock text;
    text.kind = ir::ContentKind::Text;
    text.text = "hi";
    user.content.push_back(text);
    req.messages.push_back(user);

    const json thinking =
        json_object_at(anthropic.serialize_request(req), "thinking");
    const int medium = anthropic_budget_for_effort(reg, "medium");
    check(json_string_at(thinking, "type") == "enabled",
          "P-13: thinking keeps its enabled type");
    check(thinking.contains("budget_tokens"),
          "P-13: a missing budget_tokens is filled in");
    check(thinking.value("budget_tokens", 0) == medium,
          "P-13: the fallback is the medium thinking budget");
}

// 01-P-17: the reasoning replay envelope carries a whitelisted payload under
// this service's own prefix, and never invents a vendor signature.
static void test_audit_p17_reasoning_envelope(const CodecRegistry & /*reg*/) {
    printf("--- 01-P-17 reasoning replay envelopes ---\n");
    const std::string openai_prefix = "token-board-openai-reasoning-v1:";
    const std::string anthropic_prefix = "token-board-anthropic-thinking-v1:";

    {  // encrypted_content only → empty thinking block carried by the envelope.
        json item = {{"type", "reasoning"},
                     {"id", "r1"},
                     {"encrypted_content", "cipher"},
                     {"status", "completed"}};
        const json block = fmt::anthropic_block_from_responses_reasoning(item);
        check(block.is_object() && json_string_at(block, "type") == "thinking",
              "P-17: encrypted-only reasoning becomes a thinking block");
        check(json_string_at(block, "thinking").empty(),
              "P-17: the carried thinking text is empty");
        const std::string signature = json_string_at(block, "signature");
        check(signature.rfind(openai_prefix, 0) == 0,
              "P-17: the signature is this service's own envelope");
        json decoded;
        check(fmt::decode_reasoning_envelope(signature, openai_prefix.c_str(),
                                             decoded),
              "P-17: the envelope decodes with the matching prefix");
        check(json_string_at(decoded, "encrypted_content") == "cipher",
              "P-17: the encrypted payload survives the envelope");
        check(!decoded.contains("status"),
              "P-17: fields outside the whitelist are not carried");
    }
    {  // summary only → unsigned thinking block.
        json item = {{"type", "reasoning"},
                     {"id", "r2"},
                     {"summary", json::array({json{{"type", "summary_text"},
                                                   {"text", "why"}}})}};
        const json block = fmt::anthropic_block_from_responses_reasoning(item);
        check(block.is_object() && json_string_at(block, "type") == "thinking",
              "P-17: summary-only reasoning becomes a thinking block");
        check(json_string_at(block, "thinking") == "why",
              "P-17: the summary text is carried");
        check(!block.contains("signature"),
              "P-17: no envelope without encrypted content");
    }
    {  // nothing to carry → dropped.
        json item = {{"type", "reasoning"}, {"id", "r3"}};
        check(fmt::anthropic_block_from_responses_reasoning(item).is_null(),
              "P-17: reasoning with neither text nor ciphertext is dropped");
    }
    {  // signed Anthropic thinking → Responses encrypted_content.
        const json block = {
            {"type", "thinking"}, {"thinking", "t"}, {"signature", "sig"}};
        const json item = fmt::responses_reasoning_from_anthropic_block(block);
        const std::string encrypted = json_string_at(item, "encrypted_content");
        check(encrypted.rfind(anthropic_prefix, 0) == 0,
              "P-17: a signed thinking block is re-wrapped for Responses");
        json decoded;
        check(fmt::decode_reasoning_envelope(encrypted, anthropic_prefix.c_str(),
                                             decoded),
              "P-17: the Anthropic envelope decodes");
        check(json_string_at(decoded, "signature") == "sig",
              "P-17: the original signature survives the round trip");
    }
    {  // unsigned thinking → no envelope is invented.
        const json block = {{"type", "thinking"}, {"thinking", "t"}};
        check(!fmt::responses_reasoning_from_anthropic_block(block).contains(
                  "encrypted_content"),
              "P-17: an unsigned thinking block stays unsigned");
    }
}

// 01-P-16: a `data` line without a colon is an empty data field, and the
// `id:`/`retry:` fields are deliberately not propagated (t3 took the
// documented branch: the conversion path rebuilds the event stream).
static void test_audit_p16_sse_bare_data_and_id() {
    printf("--- 01-P-16 bare `data` line and id:/retry: fields ---\n");
    std::string event_name;
    std::string data;
    const bool bare = fmt::parse_sse_frame("data\n\n", &event_name, &data);
    check(bare, "P-16: a bare `data` line still counts as a data field");
    check(data.empty(), "P-16: the bare `data` field is empty");

    // A bare `data` followed by a normal one must not swallow the payload.
    event_name.clear();
    data.clear();
    check(fmt::parse_sse_frame("data\ndata: hello\n\n", &event_name, &data),
          "P-16: a keepalive followed by a payload parses");
    check(data == "hello", "P-16: the payload after a bare `data` survives");

    // id:/retry: are documented as not propagated: they must not be mistaken
    // for an event name nor leak into the data field.
    event_name.clear();
    data.clear();
    check(fmt::parse_sse_frame("id: 42\nretry: 1000\ndata: payload\n\n",
                               &event_name, &data),
          "P-16: a frame carrying id:/retry: still parses");
    check(event_name.empty(), "P-16: id:/retry: are not read as an event name");
    check(data == "payload", "P-16: id:/retry: leave the data field alone");
}

// 01-P-17 ⑤: the replay envelope is this service's own carrier, so it must be
// stripped before a signature-verifying Anthropic upstream sees it — and kept
// for every other upstream, where it carries cross-vendor replays.
static void test_audit_p17_strip_envelopes(const CodecRegistry &reg) {
    printf("--- 01-P-17⑤ signature-verifying target strips the carrier ---\n");
    const std::string openai_prefix = "token-board-openai-reasoning-v1:";
    const std::string anthropic_prefix = "token-board-anthropic-thinking-v1:";

    // Both carrier shapes are stripped, visible text stays, and a genuine
    // vendor signature is never touched.
    {
        json body = {{"messages", json::array({json{
            {"role", "assistant"},
            {"content", json::array({
                json{{"type", "thinking"}, {"thinking", "why"},
                     {"signature", openai_prefix + "AAA"}},
                json{{"type", "redacted_thinking"},
                     {"data", anthropic_prefix + "BBB"}},
                json{{"type", "thinking"}, {"thinking", "vendor"},
                     {"signature", "vendor-sig"}}})}}})}};
        fmt::strip_reasoning_envelopes(body);
        const json &blocks = body["messages"][0]["content"];
        check(!blocks[0].contains("signature"),
              "P-17⑤: the envelope signature is stripped");
        check(json_string_at(blocks[0], "thinking") == "why",
              "P-17⑤: the visible thinking text survives the strip");
        check(json_string_at(blocks[1], "type") == "thinking" &&
                  json_string_at(blocks[1], "thinking").empty() &&
                  !blocks[1].contains("data"),
              "P-17⑤: a carrier redacted block degrades to an empty thinking "
              "block");
        check(json_string_at(blocks[2], "signature") == "vendor-sig",
              "P-17⑤: a genuine vendor signature is left untouched");
        check(body.dump().find("token-board-") == std::string::npos,
              "P-17⑤: no carrier survives anywhere in the body");
    }

    // Wiring: the serializer strips only when the target was judged to verify
    // signatures; every other upstream keeps the envelope for replay.
    const FormatCodec &anthropic = reg.get(ApiFormat::Anthropic);
    ir::ChatRequest req;
    req.model = "m";
    req.max_tokens = 10;
    ir::Message assistant;
    assistant.role = "assistant";
    ir::ContentBlock thinking;
    thinking.kind = ir::ContentKind::Thinking;
    thinking.text = "carried";
    thinking.extra["signature"] = openai_prefix + "CCC";
    assistant.content.push_back(thinking);
    req.messages.push_back(assistant);

    ir::ConversionContext verifying;
    verifying.target_verifies_thinking_signature = true;
    const json stripped = anthropic.serialize_request(req, &verifying);
    check(stripped.dump().find("token-board-") == std::string::npos,
          "P-17⑤: a verifying target receives no carrier");

    ir::ConversionContext replaying;  // default: not a verifying upstream
    const json kept = anthropic.serialize_request(req, &replaying);
    check(kept.dump().find(openai_prefix) != std::string::npos,
          "P-17⑤: any other upstream keeps the envelope for cross-vendor "
          "replay");
}

static int self_test() {
    CodecRegistry reg;
    reg.add(make_openai_codec());
    reg.add(make_anthropic_codec());
    reg.add(make_responses_codec());
    reg.add(make_google_codec());

    printf("=== Request round-trips ===\n");
    struct ReqSample {
        ApiFormat fmt;
        const char *json;
    } reqs[] = {
        {ApiFormat::OpenAI, kOpenaiRequest},
        {ApiFormat::Anthropic, kAnthropicRequest},
        {ApiFormat::OpenAIResponses, kResponsesRequest},
        {ApiFormat::Google, kGoogleRequest},
    };
    for (auto &r : reqs) {
        for (int t = 0; t < 4; t++) {
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
        {ApiFormat::Google, kGoogleResponse},
    };
    for (auto &r : resps) {
        for (int t = 0; t < 4; t++) {
            ApiFormat to = static_cast<ApiFormat>(t);
            run_response_roundtrip(reg, r.fmt, to, r.json);
        }
    }

    printf("=== Cross-format regression tests ===\n");
    test_sse_multiline_data();
    test_audit_p01_system_fold_separator(reg);
    test_audit_p02_budget_to_effort(reg);
    test_audit_p03_effort_budget_ladder(reg);
    test_audit_p04_developer_role_system(reg);
    test_audit_p05_malformed_tool_result(reg);
    test_audit_p07_tool_media_keeps_id(reg);
    test_audit_p08_max_tokens_field(reg);
    test_audit_p09_anthropic_input_schema(reg);
    test_audit_p10_recursive_normalize(reg);
    test_audit_p12_stop_reason_roundtrip(reg);
    test_audit_p13_thinking_budget_fallback(reg);
    test_audit_p17_reasoning_envelope(reg);
    test_audit_p16_sse_bare_data_and_id();
    test_audit_p17_strip_envelopes(reg);
    test_openai_to_anthropic_stream_tool_calls(reg);
    test_openai_to_anthropic_stream_reasoning_then_text(reg);
    test_anthropic_to_openai_request_tool_result(reg);
    test_anthropic_to_openai_tool_result_with_text(reg);
    test_anthropic_to_openai_reasoning_content(reg);
    test_anthropic_to_openai_tool_choice(reg);
    test_openai_to_anthropic_tool_choice(reg);
    test_strip_one_m_suffix(reg);
    test_responses_optional_message_and_tool_choice(reg);
    test_responses_custom_tool_items(reg);
    test_responses_stream_lifecycle(reg);
    test_native_media_bridges(reg);
    test_anthropic_message_stop_only(reg);
    test_anthropic_stop_reason_survives_message_stop(reg);
    test_anthropic_tool_use_survives_message_stop(reg);
    test_openai_done_only_terminal(reg);
    test_openai_finish_then_done_dedupe(reg);
    test_terminal_frame_without_blank_line(reg);
    test_responses_terminal_without_envelope(reg);
    test_openai_done_only_tool_terminal(reg);
    test_openai_stream_opencode_usage_frame(reg);
    test_anthropic_tool_message_stop_only(reg);
    test_anthropic_target_usage_mirror(reg);
    test_openai_target_usage_fold(reg);
    test_responses_completed_after_items(reg);
    test_responses_tool_done_once(reg);
    test_duplicate_cycle_suppression(reg);
    test_google_finish_reason_matrix(reg);
    test_google_usage_mapping(reg);
    test_google_tool_id_and_result_name(reg);
    test_google_thinking_signature(reg);
    test_google_blocked_prompt(reg);
    test_google_error_envelope(reg);
    test_google_media_bridge(reg);
    test_google_stream_events(reg);

    printf("\n%s (%d failure%s)\n", g_failures == 0 ? "ALL PASSED" : "FAILED",
           g_failures, g_failures == 1 ? "" : "s");
    return g_failures == 0 ? 0 : 1;
}

int main(int argc, char **argv) {
    CodecRegistry reg;
    reg.add(make_openai_codec());
    reg.add(make_anthropic_codec());
    reg.add(make_responses_codec());
    reg.add(make_google_codec());

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
        if (s == "responses") return std::string("openai_responses");
        if (s == "gemini") return std::string("google");
        return s;
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
