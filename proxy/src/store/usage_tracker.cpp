#include "usage_tracker.h"
#include "db.h"
#include "format_common.h"

#include "json.hpp"

#include <cstdio>
#include <limits>
#include <sstream>

using json = nlohmann::json;

namespace {

// Usage is observability metadata and must never be able to abort forwarding.
// Accept only non-negative integers representable by the in-memory counters;
// malformed, fractional, negative, and oversized values are ignored.
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
        // Best-effort metrics parsing: invalid metadata is equivalent to absent.
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
        auto value = fmt::read_cache_hit_tokens(usage, prompt_tokens);
        return value && *value >= 0 ? *value : 0;
    } catch (const json::exception &) {
        return 0;
    }
}

}  // namespace

// ── parse_usage (non-streaming JSON) ─────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_usage(const std::string &body) {
    try {
        json j = json::parse(body);

        UsageInfo info;

        // Extract model (top-level field)
        if (j.contains("model") && j["model"].is_string())
            info.model = j["model"].get<std::string>();

        // Extract usage
        if (j.contains("usage")) {
            auto &u = j["usage"];
            read_into(u, "prompt_tokens", info.prompt_tokens);
            read_into(u, "completion_tokens", info.completion_tokens);
            read_into(u, "total_tokens", info.total_tokens);

            // Cached prompt tokens.  Upstreams report them several ways:
            // prompt_cache_hit_tokens (deepseek), prompt_tokens_details/
            // input_tokens_details.cached_tokens (OpenAI chat / Responses),
            // or not at all (minimax) → 0.  prompt_tokens already includes
            // the hits, so cache miss = prompt_tokens - cache_read_tokens.
            info.cache_read_tokens = cache_hit_tokens(u, info.prompt_tokens);

            // If total_tokens is 0 but we have prompt + completion, sum them
            if (info.total_tokens == 0)
                info.total_tokens = usage_sum(info.prompt_tokens,
                                              info.completion_tokens);
        }

        return info;
    } catch (const json::exception &e) {
        fprintf(stderr, "[Tracker] JSON usage parse error: %s\n", e.what());
        return std::nullopt;
    }
}

// ── parse_usage_from_sse ─────────────────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_usage_from_sse(const std::string &sse_data) {
    UsageInfo info;

    // Scan SSE data lines for "data:" prefixed JSON
    // We look for the last chunk that contains a "usage" field.
    std::istringstream stream(sse_data);
    std::string line;
    bool found_usage = false;

    while (std::getline(stream, line)) {
        // Trim trailing \r
        if (!line.empty() && line.back() == '\r')
            line.pop_back();

        // Skip empty lines and comments
        if (line.empty() || line[0] == ':')
            continue;

        // Accept both "data: " and "data:" (with or without space)
        std::string json_str;
        if (line.rfind("data: ", 0) == 0) {
            json_str = line.substr(6);
        } else if (line.rfind("data:", 0) == 0) {
            json_str = line.substr(5);
        } else {
            continue;
        }

        // Skip "[DONE]"
        if (json_str == "[DONE]")
            continue;

        try {
            json j = json::parse(json_str);

            // Get model from first chunk that has it
            if (info.model.empty() && j.contains("model") && j["model"].is_string())
                info.model = j["model"].get<std::string>();

            // Check for usage in this chunk
            if (j.contains("usage") && !j["usage"].is_null()) {
                auto &u = j["usage"];
                read_into(u, "prompt_tokens", info.prompt_tokens);
                read_into(u, "completion_tokens", info.completion_tokens);
                read_into(u, "total_tokens", info.total_tokens);

                // Cached prompt tokens — see parse_usage for the upstream
                // variants.  The opencode.ai inference-cost frame below
                // overrides this with its own cacheReadTokens when present.
                info.cache_read_tokens = cache_hit_tokens(u, info.prompt_tokens);

                found_usage = true;
            }

            // opencode.ai-specific usage frame (non-standard, mirrors the real
            // opencode.ai stream): `{"choices":[],"x-opencode-type":
            // "inference-cost","normalizedUsage":{inputTokens, outputTokens,
            // cacheReadTokens, cacheWrite5mTokens, cacheWrite1hTokens,...}}`.
            // It is the last usage-bearing frame, so it takes precedence.
            if (j.contains("x-opencode-type") &&
                j["x-opencode-type"] == "inference-cost" &&
                j.contains("normalizedUsage") &&
                j["normalizedUsage"].is_object()) {
                auto &nu = j["normalizedUsage"];
                read_into(nu, "inputTokens", info.prompt_tokens);
                read_into(nu, "outputTokens", info.completion_tokens);
                read_into(nu, "cacheReadTokens", info.cache_read_tokens);
                // Cache writes are billed at the input rate, matching the
                // Anthropic cache_creation semantics.
                int cache_write_5m = 0;
                int cache_write_1h = 0;
                read_into(nu, "cacheWrite5mTokens", cache_write_5m);
                read_into(nu, "cacheWrite1hTokens", cache_write_1h);
                const long long cache_writes =
                    static_cast<long long>(cache_write_5m) + cache_write_1h;
                info.cache_creation_tokens = cache_writes <= std::numeric_limits<int>::max()
                    ? static_cast<int>(cache_writes) : 0;
                read_into(nu, "totalTokens", info.total_tokens);
                found_usage = true;
            }
        } catch (const json::exception &) {
            // Skip malformed JSON or malformed metadata lines silently.
        }
    }

    if (!found_usage)
        return std::nullopt;

    if (info.total_tokens == 0)
        info.total_tokens = usage_sum(info.prompt_tokens,
                                      info.completion_tokens);

    return info;
}

// ── parse_anthropic_usage ──────────────────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_anthropic_usage(const std::string &body) {
    try {
        json j = json::parse(body);

        UsageInfo info;

        // Extract model (top-level field, same as OpenAI)
        if (j.contains("model") && j["model"].is_string())
            info.model = j["model"].get<std::string>();

        // Anthropic uses input_tokens / output_tokens
        if (j.contains("usage")) {
            auto &u = j["usage"];
            read_into(u, "input_tokens", info.prompt_tokens);
            read_into(u, "output_tokens", info.completion_tokens);
            // Also check cache_read_input_tokens and cache_creation_input_tokens
            // for total accuracy when the API provides them.  They are kept in
            // prompt_tokens (input_tokens does not include them) and tracked
            // separately for cache-aware billing.
            read_into(u, "cache_read_input_tokens", info.cache_read_tokens);
            read_into(u, "cache_creation_input_tokens",
                      info.cache_creation_tokens);
            info.prompt_tokens = usage_sum(info.prompt_tokens,
                                           info.cache_read_tokens,
                                           info.cache_creation_tokens);
        }

        // Anthropic returns total tokens in message_delta for streaming,
        // but non-streaming may also have it
        info.total_tokens = usage_sum(info.prompt_tokens,
                                      info.completion_tokens);

        return info;
    } catch (const json::exception &e) {
        fprintf(stderr, "[Tracker] Anthropic usage parse error: %s\n", e.what());
        return std::nullopt;
    }
}

// ── parse_anthropic_usage_from_sse ────────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_anthropic_usage_from_sse(const std::string &sse_data) {
    UsageInfo info;
    bool found_usage = false;

    // Scan SSE data lines for "data:" prefixed JSON
    // Anthropic streaming events:
    //   message_start: contains message.model, optional usage
    //   content_block_start/delta/stop: content delivery
    //   message_delta: contains usage.{input_tokens, output_tokens}
    //   message_stop: end marker
    std::istringstream stream(sse_data);
    std::string line;

    while (std::getline(stream, line)) {
        // Trim trailing \r
        if (!line.empty() && line.back() == '\r')
            line.pop_back();

        if (line.empty() || line[0] == ':')
            continue;

        // Accept both "data: " and "data:"
        std::string json_str;
        if (line.rfind("data: ", 0) == 0) {
            json_str = line.substr(6);
        } else if (line.rfind("data:", 0) == 0) {
            json_str = line.substr(5);
        } else {
            continue;
        }

        try {
            json j = json::parse(json_str);

            // Extract model from message_start event
            if (info.model.empty() && j.contains("type") &&
                j["type"] == "message_start" &&
                j.contains("message") && j["message"].is_object() &&
                j["message"].contains("model") &&
                j["message"]["model"].is_string()) {
                info.model = j["message"]["model"].get<std::string>();
            }

            // Extract usage from message_delta event (has the final usage)
            if (j.contains("type") && j["type"] == "message_delta" &&
                j.contains("usage")) {
                auto &u = j["usage"];
                read_into(u, "input_tokens", info.prompt_tokens);
                read_into(u, "output_tokens", info.completion_tokens);
                read_into(u, "cache_read_input_tokens",
                          info.cache_read_tokens);
                read_into(u, "cache_creation_input_tokens",
                          info.cache_creation_tokens);
                info.prompt_tokens = usage_sum(info.prompt_tokens,
                                               info.cache_read_tokens,
                                               info.cache_creation_tokens);
                found_usage = true;
            }
        } catch (const json::exception &) {
            // Skip malformed JSON or malformed metadata lines silently.
        }
    }

    if (!found_usage)
        return std::nullopt;

    info.total_tokens = usage_sum(info.prompt_tokens,
                                  info.completion_tokens);
    return info;
}

// ── parse_responses_usage ─────────────────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_responses_usage(const std::string &body) {
    try {
        json j = json::parse(body);
        UsageInfo info;
        if (j.contains("model") && j["model"].is_string())
            info.model = j["model"].get<std::string>();
        if (j.contains("usage")) {
            auto &u = j["usage"];
            read_into(u, "input_tokens", info.prompt_tokens);
            read_into(u, "output_tokens", info.completion_tokens);
            if (auto total = usage_int(u, "total_tokens"))
                info.total_tokens = *total;
            else
                info.total_tokens = usage_sum(info.prompt_tokens,
                                              info.completion_tokens);
            // Cached prompt tokens — see parse_usage for the upstream
            // variants (Responses format: input_tokens_details.cached_tokens
            // or prompt_cache_hit_tokens / miss-derived).
            info.cache_read_tokens = cache_hit_tokens(u, info.prompt_tokens);
        }
        if (info.total_tokens == 0)
            info.total_tokens = usage_sum(info.prompt_tokens,
                                          info.completion_tokens);
        return info;
    } catch (const json::exception &e) {
        fprintf(stderr, "[Tracker] Responses usage parse error: %s\n", e.what());
        return std::nullopt;
    }
}

// ── parse_responses_usage_from_sse ────────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_responses_usage_from_sse(const std::string &sse_data) {
    UsageInfo info;
    bool found_usage = false;
    std::istringstream stream(sse_data);
    std::string line;

    while (std::getline(stream, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == ':') continue;
        std::string json_str;
        if (line.rfind("data: ", 0) == 0) json_str = line.substr(6);
        else if (line.rfind("data:", 0) == 0) json_str = line.substr(5);
        else continue;
        try {
            json j = json::parse(json_str);
            if (info.model.empty() && j.contains("response") &&
                j["response"].is_object() && j["response"].contains("model") &&
                j["response"]["model"].is_string())
                info.model = j["response"]["model"].get<std::string>();
            // Usage lives in response.completed / response.incomplete.
            if ((j.contains("type") && (j["type"] == "response.completed" ||
                                        j["type"] == "response.incomplete")) &&
                j.contains("response") && j["response"].is_object() &&
                j["response"].contains("usage")) {
                auto &u = j["response"]["usage"];
                read_into(u, "input_tokens", info.prompt_tokens);
                read_into(u, "output_tokens", info.completion_tokens);
                if (auto total = usage_int(u, "total_tokens"))
                    info.total_tokens = *total;
                else
                    info.total_tokens = usage_sum(info.prompt_tokens,
                                                  info.completion_tokens);
                // Cached prompt tokens — see parse_usage for the upstream
                // variants (Responses format: input_tokens_details.cached_tokens
                // or prompt_cache_hit_tokens / miss-derived).
                info.cache_read_tokens = cache_hit_tokens(u, info.prompt_tokens);
                found_usage = true;
            }
        } catch (const json::exception &) {
            // Skip malformed JSON or malformed metadata lines silently.
        }
    }

    if (!found_usage) return std::nullopt;
    if (info.total_tokens == 0)
        info.total_tokens = usage_sum(info.prompt_tokens,
                                      info.completion_tokens);
    return info;
}

// ── parse_stream_usage (dispatcher) ───────────────────────────────────────

std::optional<UsageTracker::UsageInfo>
UsageTracker::parse_stream_usage(const std::string &api_format,
                                 const std::string &sse_data) {
    if (api_format == "anthropic")
        return parse_anthropic_usage_from_sse(sse_data);
    if (api_format == "openai_responses")
        return parse_responses_usage_from_sse(sse_data);
    return parse_usage_from_sse(sse_data);
}

// ── log_request ──────────────────────────────────────────────────────────

bool UsageTracker::log_request(int account_id, int local_key_id,
                               const UsageInfo &usage,
                               bool is_streaming, int status_code,
                               int duration_ms, int upstream_key_id,
                               int ttft_ms, int generation_ms, double output_tps,
                               int upstream_ttft_ms, int upstream_duration_ms,
                               int attempt_count,
                               const std::vector<Database::AttemptInfo> &attempts,
                               double *out_cost) {
    // Cost is computed automatically by the tr_request_log_insert SQLite
    // trigger, so we pass 0.0 here. The trigger reads model_pricing and
    // sets the correct api_cost immediately after insert.
    double cost = 0.0;

    const bool accepted = db_.log_request(
        account_id, local_key_id, usage.model,
        usage.prompt_tokens, usage.completion_tokens,
        usage.cache_read_tokens, usage.total_tokens, cost,
        is_streaming, status_code, duration_ms, upstream_key_id,
        ttft_ms, generation_ms, output_tps, upstream_ttft_ms,
        upstream_duration_ms, attempt_count, attempts, out_cost);
    if (!accepted) {
        fprintf(stderr,
                "[Tracker] request log rejected/failed: account=%d model=%s "
                "status=%d\n",
                account_id, usage.model.c_str(), status_code);
    }
    return accepted;
}
