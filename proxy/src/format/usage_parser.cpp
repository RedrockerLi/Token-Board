#include "usage_parser.h"

#include "format_common.h"
#include "json.hpp"

#include <limits>
#include <sstream>

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

std::string data_line(const std::string &line) {
    if (line.rfind("data: ", 0) == 0) return line.substr(6);
    if (line.rfind("data:", 0) == 0) return line.substr(5);
    return {};
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
    // Historical non-streaming accounting total included both cache buckets.
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

std::optional<WireUsage> parse_openai_usage_sse(const std::string &sse_data) {
    WireUsage result;
    bool found_usage = false;
    std::istringstream stream(sse_data);
    std::string line;
    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == ':') continue;
        const std::string payload = data_line(line);
        if (payload.empty() || payload == "[DONE]") continue;
        try {
            const json root = json::parse(payload);
            model_from(root, result.model);
            if (root.contains("usage") && !root["usage"].is_null()) {
                openai_usage_object(root["usage"], result.usage);
                found_usage = true;
            }
            // OpenCode's inference-cost frame is a protocol extension, not a
            // database concern. It remains the final usage-bearing frame.
            if (root.contains("x-opencode-type") &&
                root["x-opencode-type"] == "inference-cost" &&
                root.contains("normalizedUsage") &&
                root["normalizedUsage"].is_object()) {
                const auto &usage = root["normalizedUsage"];
                read_into(usage, "inputTokens", result.usage.prompt_tokens);
                read_into(usage, "outputTokens", result.usage.completion_tokens);
                read_into(usage, "cacheReadTokens", result.usage.cache_read_tokens);
                int write_5m = 0;
                int write_1h = 0;
                read_into(usage, "cacheWrite5mTokens", write_5m);
                read_into(usage, "cacheWrite1hTokens", write_1h);
                const long long writes = static_cast<long long>(write_5m) + write_1h;
                result.usage.cache_creation_tokens =
                    writes <= std::numeric_limits<int>::max()
                        ? static_cast<int>(writes) : 0;
                read_into(usage, "totalTokens", result.usage.total_tokens);
                found_usage = true;
            }
        } catch (const json::exception &) {
            // A malformed observability frame does not abort forwarding.
        }
    }
    if (!found_usage) return std::nullopt;
    if (result.usage.total_tokens == 0)
        result.usage.total_tokens = usage_sum(result.usage.prompt_tokens,
                                              result.usage.completion_tokens);
    return result;
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

std::optional<WireUsage> parse_anthropic_usage_sse(const std::string &sse_data) {
    WireUsage result;
    bool found_usage = false;
    std::istringstream stream(sse_data);
    std::string line;
    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == ':') continue;
        const std::string payload = data_line(line);
        if (payload.empty()) continue;
        try {
            const json root = json::parse(payload);
            if (root.value("type", "") == "message_start" &&
                root.contains("message") && root["message"].is_object())
                model_from(root["message"], result.model);
            if (root.value("type", "") == "message_delta" &&
                root.contains("usage") && root["usage"].is_object()) {
                anthropic_usage_object(root["usage"], result.usage);
                found_usage = true;
            }
        } catch (const json::exception &) {
        }
    }
    return found_usage ? std::optional<WireUsage>(result) : std::nullopt;
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

std::optional<WireUsage> parse_responses_usage_sse(const std::string &sse_data) {
    WireUsage result;
    bool found_usage = false;
    std::istringstream stream(sse_data);
    std::string line;
    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == ':') continue;
        const std::string payload = data_line(line);
        if (payload.empty()) continue;
        try {
            const json root = json::parse(payload);
            if (root.contains("response") && root["response"].is_object()) {
                model_from(root["response"], result.model);
                const std::string type = root.value("type", "");
                if ((type == "response.completed" || type == "response.incomplete") &&
                    root["response"].contains("usage") &&
                    root["response"]["usage"].is_object()) {
                    responses_usage_object(root["response"]["usage"], result.usage);
                    found_usage = true;
                }
            }
        } catch (const json::exception &) {
        }
    }
    if (!found_usage) return std::nullopt;
    if (result.usage.total_tokens == 0)
        result.usage.total_tokens = usage_sum(result.usage.prompt_tokens,
                                              result.usage.completion_tokens);
    return result;
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

std::optional<WireUsage> parse_stream_usage_for_format(
    ir::ApiFormat format, const std::string &sse_data) {
    switch (format) {
    case ir::ApiFormat::Anthropic:
        return parse_anthropic_usage_sse(sse_data);
    case ir::ApiFormat::OpenAIResponses:
        return parse_responses_usage_sse(sse_data);
    case ir::ApiFormat::OpenAI:
        return parse_openai_usage_sse(sse_data);
    }
    return std::nullopt;
}

}  // namespace fmt
