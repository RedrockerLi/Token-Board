#include "usage_accounting.h"

#include <cassert>

int main() {
    ir::Usage wire;
    wire.prompt_tokens = 100;
    wire.completion_tokens = 20;
    wire.cache_read_tokens = 30;
    wire.cache_creation_tokens = 5;
    wire.total_tokens = 120;

    const auto openai = UsageAccounting::from_ir(
        wire, ir::ApiFormat::OpenAI, "gpt-test");
    assert(openai.prompt_tokens == 100);
    assert(openai.cache_read_tokens == 30);
    assert(openai.cache_creation_tokens == 5);
    assert(openai.total_tokens == 120);

    // Database compatibility projection: Anthropic's wire input excludes
    // cache buckets, so the existing prompt field folds both back in.  The
    // stored total is recomputed from the folded prompt: 135 + 20.
    const auto anthropic = UsageAccounting::from_ir(
        wire, ir::ApiFormat::Anthropic, "claude-test");
    assert(anthropic.prompt_tokens == 135);
    assert(anthropic.completion_tokens == 20);
    assert(anthropic.cache_read_tokens == 30);
    assert(anthropic.cache_creation_tokens == 5);
    assert(anthropic.total_tokens == 155);

    // Anthropic streaming shape: the IR total carries the mutually-exclusive
    // net input plus completion (8 + 2 = 10) while the cache buckets sit
    // beside it.  Forwarding that IR total would under-count the stored row by
    // every cached token; the projection must fold the buckets into prompt and
    // recompute: prompt 8 + 30 + 4 = 42, total 42 + 2 = 44.
    ir::Usage stream_wire;
    stream_wire.prompt_tokens = 8;
    stream_wire.completion_tokens = 2;
    stream_wire.cache_read_tokens = 30;
    stream_wire.cache_creation_tokens = 4;
    stream_wire.total_tokens = 10;

    const auto stream_openai = UsageAccounting::from_ir(
        stream_wire, ir::ApiFormat::OpenAI, "gpt-test");
    assert(stream_openai.prompt_tokens == 8);
    assert(stream_openai.total_tokens == 10);

    const auto stream_anthropic = UsageAccounting::from_ir(
        stream_wire, ir::ApiFormat::Anthropic, "claude-test");
    assert(stream_anthropic.prompt_tokens == 42);
    assert(stream_anthropic.completion_tokens == 2);
    assert(stream_anthropic.cache_read_tokens == 30);
    assert(stream_anthropic.cache_creation_tokens == 4);
    assert(stream_anthropic.total_tokens == 44);

    // A provider-authored total that disagrees with its own buckets is not
    // stored: the row always carries prompt + completion (125 -> 120), for
    // every format.
    ir::Usage authored_total;
    authored_total.prompt_tokens = 100;
    authored_total.completion_tokens = 20;
    authored_total.total_tokens = 125;
    const auto authored = UsageAccounting::from_ir(
        authored_total, ir::ApiFormat::OpenAI, "gpt-test");
    assert(authored.total_tokens == 120);

    // An out-of-range sum falls back to 0 instead of overflowing.
    ir::Usage oversized;
    oversized.prompt_tokens = 2000000000;
    oversized.completion_tokens = 2000000000;
    const auto clamped = UsageAccounting::from_ir(
        oversized, ir::ApiFormat::OpenAI, "gpt-test");
    assert(clamped.total_tokens == 0);
    return 0;
}
