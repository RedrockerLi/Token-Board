#include "usage_parser.h"

#include "format_common.h"
#include "json.hpp"

#include <limits>

using json = nlohmann::json;

namespace fmt {
namespace {

// Usage is observability metadata and must never be able to abort forwarding.
// Keep the old accepted domain: non-negative integers representable by the
// in-memory counters. Fractional, negative, malformed and oversized values
// are treated as absent.
std::optional<int> usage_int(const json &object, const char *key) noexcept {
    if (!object.is_object()) return std::nullopt;
    auto it = object.find(key);
    if (it == object.end()) return std::nullopt;
    try {
        if (it->is_number_unsigned()) {
            const auto value = it->get<unsigned long long>();
            if (value <= static_cast<unsigned long long>(
                             std::numeric_limits<int>::max()))
                return static_cast<int>(value);
            return std::nullopt;
        }
        if (it->is_number_integer()) {
            const auto value = it->get<long long>();
            if (value >= 0 && value <= std::numeric_limits<int>::max())
                return static_cast<int>(value);
        }
    } catch (const json::exception &) {
    }
    return std::nullopt;
}

void read_into(const json &object, const char *key, int &target) noexcept {
    if (auto value = usage_int(object, key)) target = *value;
}

int usage_sum(int first, int second, int third = 0) noexcept {
    const long long total = static_cast<long long>(first) + second + third;
    return total >= 0 && total <= std::numeric_limits<int>::max()
        ? static_cast<int>(total) : 0;
}

int cache_hit_tokens(const json &usage, int prompt_tokens) noexcept {
    try {
        auto value = read_cache_hit_tokens(usage, prompt_tokens);
        return value && *value >= 0 ? *value : 0;
    } catch (const json::exception &) {
        return 0;
    }
}

void model_from(const json &object, std::string &model) {
    if (model.empty() && object.is_object() && object.contains("model") &&
        object["model"].is_string())
        model = object["model"].get<std::string>();
}

void openai_usage_object(const json &usage, ir::Usage &out) {
    read_into(usage, "prompt_tokens", out.prompt_tokens);
    read_into(usage, "completion_tokens", out.completion_tokens);
    read_into(usage, "total_tokens", out.total_tokens);
    out.cache_read_tokens = cache_hit_tokens(usage, out.prompt_tokens);
    if (out.total_tokens == 0)
        out.total_tokens = usage_sum(out.prompt_tokens, out.completion_tokens);
}

void anthropic_usage_object(const json &usage, ir::Usage &out) {
    read_into(usage, "input_tokens", out.prompt_tokens);
    read_into(usage, "output_tokens", out.completion_tokens);
    read_into(usage, "cache_read_input_tokens", out.cache_read_tokens);
    read_into(usage, "cache_creation_input_tokens", out.cache_creation_tokens);
    // Keep IR prompt_tokens in the provider's mutually-exclusive input
    // bucket. UsageAccounting performs the single compatibility fold for the
    // database projection; total_tokens nevertheless preserves the historical
    // non-streaming accounting total, which included both cache buckets.
    const int total_input = usage_sum(out.prompt_tokens,
                                      out.cache_read_tokens,
                                      out.cache_creation_tokens);
    out.total_tokens = usage_sum(total_input, out.completion_tokens);
}

void responses_usage_object(const json &usage, ir::Usage &out) {
    read_into(usage, "input_tokens", out.prompt_tokens);
    read_into(usage, "output_tokens", out.completion_tokens);
    if (auto total = usage_int(usage, "total_tokens"))
        out.total_tokens = *total;
    else
        out.total_tokens = usage_sum(out.prompt_tokens, out.completion_tokens);
    out.cache_read_tokens = cache_hit_tokens(usage, out.prompt_tokens);
    if (out.total_tokens == 0)
        out.total_tokens = usage_sum(out.prompt_tokens, out.completion_tokens);
}

}  // namespace

std::optional<WireUsage> parse_openai_usage_json(const std::string &body) {
    try {
        const json root = json::parse(body);
        WireUsage result;
        model_from(root, result.model);
        if (root.contains("usage") && !root["usage"].is_null())
            openai_usage_object(root["usage"], result.usage);
        return result;
    } catch (const json::exception &) {
        return std::nullopt;
    }
}

std::optional<WireUsage> parse_anthropic_usage_json(const std::string &body) {
    try {
        const json root = json::parse(body);
        WireUsage result;
        model_from(root, result.model);
        if (root.contains("usage") && root["usage"].is_object())
            anthropic_usage_object(root["usage"], result.usage);
        return result;
    } catch (const json::exception &) {
        return std::nullopt;
    }
}

std::optional<WireUsage> parse_responses_usage_json(const std::string &body) {
    try {
        const json root = json::parse(body);
        WireUsage result;
        model_from(root, result.model);
        if (root.contains("usage") && root["usage"].is_object())
            responses_usage_object(root["usage"], result.usage);
        return result;
    } catch (const json::exception &) {
        return std::nullopt;
    }
}

std::optional<WireUsage> parse_usage_for_format(ir::ApiFormat format,
                                                const std::string &body) {
    switch (format) {
    case ir::ApiFormat::Anthropic:
        return parse_anthropic_usage_json(body);
    case ir::ApiFormat::OpenAIResponses:
        return parse_responses_usage_json(body);
    case ir::ApiFormat::OpenAI:
        return parse_openai_usage_json(body);
    }
    return std::nullopt;
}

}  // namespace fmt
