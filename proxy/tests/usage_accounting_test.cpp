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
    // cache buckets, so the existing prompt field folds both back in.
    const auto anthropic = UsageAccounting::from_ir(
        wire, ir::ApiFormat::Anthropic, "claude-test");
    assert(anthropic.prompt_tokens == 135);
    assert(anthropic.completion_tokens == 20);
    assert(anthropic.cache_read_tokens == 30);
    assert(anthropic.cache_creation_tokens == 5);
    assert(anthropic.total_tokens == 120);
    return 0;
}
