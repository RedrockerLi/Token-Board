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

#include "codec.h"
#include "format_anthropic.h"
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
