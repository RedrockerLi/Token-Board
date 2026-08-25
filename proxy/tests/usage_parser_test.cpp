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

    const auto responses = fmt::parse_stream_usage_for_format(
        ir::ApiFormat::OpenAIResponses,
        "data: {\"type\":\"response.completed\",\"response\":{\"model\":\"r-test\",\"usage\":{\"input_tokens\":7,\"output_tokens\":2,\"input_tokens_details\":{\"cached_tokens\":4}}}}\n\n");
    assert(responses.has_value());
    assert(responses->model == "r-test");
    assert(responses->usage.prompt_tokens == 7);
    assert(responses->usage.cache_read_tokens == 4);
    assert(responses->usage.total_tokens == 9);

    assert(!fmt::parse_usage_for_format(ir::ApiFormat::OpenAI,
                                        "not-json").has_value());
    assert(!fmt::parse_stream_usage_for_format(ir::ApiFormat::Anthropic,
                                               "data: {broken}\n\n").has_value());
    return 0;
}
