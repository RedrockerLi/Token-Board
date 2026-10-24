/// Standalone checks for fmt::SseCycleGuard — the passthrough guard that
/// delivers exactly one message cycle per upstream response.
///
/// Exit code 0 = every case passed.

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "ir.h"
#include "sse_cycle_guard.h"

namespace {

int g_failures = 0;

void check(bool ok, const char *what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL: %s\n", what);
        ++g_failures;
    }
}

struct Result {
    std::string out;
    bool terminal = false;
    bool terminal_frame = false;
    bool suppressed = false;
    bool failed = false;
    std::size_t dropped = 0;
};

Result run(const ir::ApiFormat format, const std::string &input,
           std::size_t chunk, std::size_t max_frame = fmt::kMaxSseFrameBytes) {
    fmt::SseCycleGuard guard(format, max_frame);
    Result r;
    for (std::size_t i = 0; i < input.size(); i += chunk) {
        const std::size_t n = std::min(chunk, input.size() - i);
        guard.feed(input.data() + i, n, r.out);
    }
    guard.finish(r.out);
    r.terminal = guard.terminal_seen();
    r.terminal_frame = guard.terminal_frame_seen();
    r.suppressed = guard.suppressed_after_terminal();
    r.failed = guard.failed();
    r.dropped = guard.dropped_bytes();
    return r;
}

std::string to_crlf(const std::string &in) {
    std::string out;
    out.reserve(in.size() * 2);
    for (const char ch : in) {
        if (ch == '\n') out += '\r';
        out += ch;
    }
    return out;
}

const char *kAnthropic1 =
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":"
    "{\"id\":\"m1\"}}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\","
    "\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"first\"}}\n\n"
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":"
    "{\"stop_reason\":\"end_turn\"}}\n\n"
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

const char *kAnthropic2 =
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":"
    "{\"id\":\"m2\"}}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\","
    "\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"second\"}}\n\n"
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n";

const char *kOpenai1 =
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"role\":\"assistant\"}}]}\n\n"
    "data: {\"id\":\"c1\",\"choices\":[{\"index\":0,\"delta\":{},"
    "\"finish_reason\":\"stop\"}],\"usage\":{\"completion_tokens\":7}}\n\n"
    "data: [DONE]\n\n";

const char *kOpenai2 =
    "data: {\"id\":\"c2\",\"choices\":[{\"index\":0,\"delta\":"
    "{\"role\":\"assistant\"}}]}\n\n"
    "data: [DONE]\n\n";

const char *kResponses1 =
    "event: response.created\ndata: {\"type\":\"response.created\","
    "\"response\":{\"id\":\"r1\"}}\n\n"
    "event: response.completed\ndata: {\"type\":\"response.completed\","
    "\"response\":{\"id\":\"r1\",\"status\":\"completed\"}}\n\n";

const char *kResponses2 =
    "event: response.created\ndata: {\"type\":\"response.created\","
    "\"response\":{\"id\":\"r2\"}}\n\n"
    "event: response.completed\ndata: {\"type\":\"response.completed\","
    "\"response\":{\"id\":\"r2\",\"status\":\"completed\"}}\n\n";

}  // namespace

int main() {
    // Two complete Anthropic cycles: only the first is delivered, intact.
    {
        const std::string input = std::string(kAnthropic1) + kAnthropic2;
        const Result r = run(ir::ApiFormat::Anthropic, input, input.size());
        check(r.out == kAnthropic1, "anthropic: first cycle forwarded verbatim");
        check(r.terminal, "anthropic: terminal seen");
        check(r.terminal_frame, "anthropic: terminal frame delivered");
        check(r.suppressed, "anthropic: second cycle suppressed");
        check(r.dropped > 0, "anthropic: dropped bytes counted");
    }
    // One byte at a time: buffering across chunks must not change the result.
    {
        const std::string input = std::string(kAnthropic1) + kAnthropic2;
        const Result r = run(ir::ApiFormat::Anthropic, input, 1);
        check(r.out == kAnthropic1, "anthropic: byte-at-a-time output matches");
        check(r.suppressed, "anthropic: byte-at-a-time suppression");
    }
    // Every two-chunk split point.
    {
        const std::string input = std::string(kAnthropic1) + kAnthropic2;
        bool all_ok = true;
        for (std::size_t cut = 1; cut < input.size(); ++cut) {
            fmt::SseCycleGuard guard(ir::ApiFormat::Anthropic);
            std::string out;
            guard.feed(input.data(), cut, out);
            guard.feed(input.data() + cut, input.size() - cut, out);
            guard.finish(out);
            if (out != kAnthropic1) all_ok = false;
        }
        check(all_ok, "anthropic: every split point yields the first cycle");
    }
    // A single clean cycle is forwarded unchanged and is not "suppressed".
    {
        const std::string input = kAnthropic1;
        const Result r = run(ir::ApiFormat::Anthropic, input, 7);
        check(r.out == input, "anthropic: single cycle forwarded verbatim");
        check(r.terminal, "anthropic: single cycle terminal");
        check(!r.suppressed, "anthropic: single cycle is not suppression");
        check(r.dropped == 0, "anthropic: single cycle drops nothing");
    }
    // CRLF separators are preserved byte for byte.
    {
        const std::string first = to_crlf(kAnthropic1);
        const std::string input = first + to_crlf(kAnthropic2);
        const Result r = run(ir::ApiFormat::Anthropic, input, 5);
        check(r.out == first, "anthropic: CRLF first cycle preserved");
        check(r.suppressed, "anthropic: CRLF second cycle suppressed");
    }
    // Upstream omitted message_stop: the next message_start is the cut point.
    {
        const std::string no_stop =
            std::string(kAnthropic1).substr(
                0, std::string(kAnthropic1).rfind(
                       "event: message_stop"));
        const std::string input = no_stop + kAnthropic2;
        const Result r = run(ir::ApiFormat::Anthropic, input, 3);
        check(r.out == no_stop, "anthropic: cut before second message_start");
        check(r.terminal, "anthropic: second start counts as terminal");
        check(!r.terminal_frame,
              "anthropic: cut at second start is not a complete cycle");
        check(r.suppressed, "anthropic: second start suppressed");
    }
    // OpenAI: [DONE] ends the cycle; the usage chunk before it is kept.
    {
        const std::string input = std::string(kOpenai1) + kOpenai2;
        const Result r = run(ir::ApiFormat::OpenAI, input, input.size());
        check(r.out == kOpenai1, "openai: first cycle with [DONE] kept");
        check(r.terminal, "openai: [DONE] is terminal");
        check(r.suppressed, "openai: second cycle suppressed");
    }
    // OpenAI without [DONE]: finish_reason-only termination, second id cut.
    {
        const std::string first =
            std::string(kOpenai1).substr(0, std::string(kOpenai1).find("data: [DONE]"));
        const std::string input = first + kOpenai2;
        const Result r = run(ir::ApiFormat::OpenAI, input, 4);
        check(r.out == first, "openai: cut before second completion id");
        check(r.terminal, "openai: finish_reason is the EOF fallback terminal");
        check(r.terminal_frame, "openai: finish_reason completes the cycle");
        check(r.suppressed, "openai: second id suppressed");
    }
    // Responses: response.created twice, one terminal.
    {
        const std::string input = std::string(kResponses1) + kResponses2;
        const Result r = run(ir::ApiFormat::OpenAIResponses, input, 2);
        check(r.out == kResponses1, "responses: first response kept");
        check(r.terminal, "responses: completed is terminal");
        check(r.suppressed, "responses: second response.created suppressed");
    }
    // No terminal at all: the guard is transparent.
    {
        const std::string input = "event: ping\ndata: {}\n\n";
        const Result r = run(ir::ApiFormat::Anthropic, input, 2);
        check(r.out == input, "no terminal: bytes unchanged");
        check(!r.terminal, "no terminal: terminal stays false");
        check(!r.suppressed, "no terminal: nothing suppressed");
    }
    // Oversized frame: fail open and stop guarding rather than stalling.
    {
        std::string big(200, 'A');
        const Result r = run(ir::ApiFormat::Anthropic, big, big.size(), 32);
        check(r.failed, "oversized frame: guard reports failure");
        check(r.out == big, "oversized frame: bytes pass through");
    }

    if (g_failures != 0) {
        std::fprintf(stderr, "%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("sse_cycle_guard: all checks passed\n");
    return 0;
}
