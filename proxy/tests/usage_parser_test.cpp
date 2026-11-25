#include "usage_accounting.h"
#include "usage_parser.h"

#include <cassert>

int main() {
    const auto openai = fmt::parse_usage_for_format(
        ir::ApiFormat::OpenAI,
        R"({"model":"gpt-test","usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"prompt_tokens_details":{"cached_tokens":30}}})");
    assert(openai.has_value());
    assert(openai->model == "gpt-test");
    assert(openai->usage.prompt_tokens == 100);
    assert(openai->usage.cache_read_tokens == 30);
    assert(openai->usage.total_tokens == 120);

    const auto anthropic = fmt::parse_usage_for_format(
        ir::ApiFormat::Anthropic,
        R"({"model":"claude-test","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":3,"cache_creation_input_tokens":2}})");
    assert(anthropic.has_value());
    assert(anthropic->usage.prompt_tokens == 10);
    assert(anthropic->usage.cache_read_tokens == 3);
    assert(anthropic->usage.cache_creation_tokens == 2);
    assert(anthropic->usage.total_tokens == 20);
    const auto projected = UsageAccounting::from_ir(
        anthropic->usage, ir::ApiFormat::Anthropic, anthropic->model);
    assert(projected.prompt_tokens == 15);
    assert(projected.total_tokens == 20);

    const auto responses = fmt::parse_usage_for_format(
        ir::ApiFormat::OpenAIResponses,
        R"({"model":"r-test","usage":{"input_tokens":7,"output_tokens":2,"input_tokens_details":{"cached_tokens":4}}})");
    assert(responses.has_value());
    assert(responses->model == "r-test");
    assert(responses->usage.prompt_tokens == 7);
    assert(responses->usage.cache_read_tokens == 4);
    assert(responses->usage.total_tokens == 9);

    // Gemini: promptTokenCount already includes the cached content, and
    // thinking tokens are billed as output.
    const auto google = fmt::parse_usage_for_format(
        ir::ApiFormat::Google,
        R"({"modelVersion":"gemini-2.5-pro-002","usageMetadata":{"promptTokenCount":100,"cachedContentTokenCount":30,"candidatesTokenCount":20,"thoughtsTokenCount":5,"totalTokenCount":125}})");
    assert(google.has_value());
    assert(google->model == "gemini-2.5-pro-002");
    assert(google->usage.prompt_tokens == 100);
    assert(google->usage.cache_read_tokens == 30);
    assert(google->usage.cache_creation_tokens == 0);
    assert(google->usage.completion_tokens == 25);
    assert(google->usage.total_tokens == 125);
    const auto google_projected = UsageAccounting::from_ir(
        google->usage, ir::ApiFormat::Google, google->model);
    // This wire's input is cache-inclusive, so no fold is applied.
    assert(google_projected.prompt_tokens == 100);
    assert(google_projected.total_tokens == 125);

    assert(!fmt::parse_usage_for_format(ir::ApiFormat::OpenAI,
                                        "not-json").has_value());
    return 0;
}
