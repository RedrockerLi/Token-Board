#include "usage_tracker.h"
#include "db.h"

#include "json.hpp"

#include <cstdio>
#include <sstream>

using json = nlohmann::json;

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
            if (u.contains("prompt_tokens"))
                info.prompt_tokens = u["prompt_tokens"].get<int>();
            if (u.contains("completion_tokens"))
                info.completion_tokens = u["completion_tokens"].get<int>();
            if (u.contains("total_tokens"))
                info.total_tokens = u["total_tokens"].get<int>();

            // Cached prompt tokens (OpenAI chat: usage.prompt_tokens_details).
            // prompt_tokens already includes these, so the cache miss count is
            // prompt_tokens - cache_read_tokens.
            if (u.contains("prompt_tokens_details") &&
                u["prompt_tokens_details"].is_object() &&
                u["prompt_tokens_details"].contains("cached_tokens") &&
                u["prompt_tokens_details"]["cached_tokens"].is_number_integer())
                info.cache_read_tokens =
                    u["prompt_tokens_details"]["cached_tokens"].get<int>();

            // If total_tokens is 0 but we have prompt + completion, sum them
            if (info.total_tokens == 0)
                info.total_tokens = info.prompt_tokens + info.completion_tokens;
        }

        return info;
    } catch (const json::parse_error &e) {
        fprintf(stderr, "[Tracker] JSON parse error: %s\n", e.what());
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
                if (u.contains("prompt_tokens"))
                    info.prompt_tokens = u["prompt_tokens"].get<int>();
                if (u.contains("completion_tokens"))
                    info.completion_tokens = u["completion_tokens"].get<int>();
                if (u.contains("total_tokens"))
                    info.total_tokens = u["total_tokens"].get<int>();

                // Cached prompt tokens (OpenAI chat: prompt_tokens_details).
                if (u.contains("prompt_tokens_details") &&
                    u["prompt_tokens_details"].is_object() &&
                    u["prompt_tokens_details"].contains("cached_tokens") &&
                    u["prompt_tokens_details"]["cached_tokens"].is_number_integer())
                    info.cache_read_tokens =
                        u["prompt_tokens_details"]["cached_tokens"].get<int>();

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
                if (nu.contains("inputTokens") && nu["inputTokens"].is_number_integer())
                    info.prompt_tokens = nu["inputTokens"].get<int>();
                if (nu.contains("outputTokens") && nu["outputTokens"].is_number_integer())
                    info.completion_tokens = nu["outputTokens"].get<int>();
                if (nu.contains("cacheReadTokens") && nu["cacheReadTokens"].is_number_integer())
                    info.cache_read_tokens = nu["cacheReadTokens"].get<int>();
                // Cache writes are billed at the input rate, matching the
                // Anthropic cache_creation semantics.
                if (nu.contains("cacheWrite5mTokens") && nu["cacheWrite5mTokens"].is_number_integer())
                    info.cache_creation_tokens += nu["cacheWrite5mTokens"].get<int>();
                if (nu.contains("cacheWrite1hTokens") && nu["cacheWrite1hTokens"].is_number_integer())
                    info.cache_creation_tokens += nu["cacheWrite1hTokens"].get<int>();
                if (nu.contains("totalTokens") && nu["totalTokens"].is_number_integer())
                    info.total_tokens = nu["totalTokens"].get<int>();
                found_usage = true;
            }
        } catch (const json::parse_error &) {
            // Skip malformed JSON lines silently
        }
    }

    if (!found_usage)
        return std::nullopt;

    if (info.total_tokens == 0)
        info.total_tokens = info.prompt_tokens + info.completion_tokens;

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
            if (u.contains("input_tokens"))
                info.prompt_tokens = u["input_tokens"].get<int>();
            if (u.contains("output_tokens"))
                info.completion_tokens = u["output_tokens"].get<int>();
            // Also check cache_read_input_tokens and cache_creation_input_tokens
            // for total accuracy when the API provides them.  They are kept in
            // prompt_tokens (input_tokens does not include them) and tracked
            // separately for cache-aware billing.
            if (u.contains("cache_read_input_tokens"))
                info.cache_read_tokens = u["cache_read_input_tokens"].get<int>();
            if (u.contains("cache_creation_input_tokens"))
                info.cache_creation_tokens = u["cache_creation_input_tokens"].get<int>();
            info.prompt_tokens += info.cache_read_tokens + info.cache_creation_tokens;
        }

        // Anthropic returns total tokens in message_delta for streaming,
        // but non-streaming may also have it
        info.total_tokens = info.prompt_tokens + info.completion_tokens;

        return info;
    } catch (const json::parse_error &e) {
        fprintf(stderr, "[Tracker] Anthropic JSON parse error: %s\n", e.what());
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
                j.contains("message") && j["message"].contains("model")) {
                info.model = j["message"]["model"].get<std::string>();
            }

            // Extract usage from message_delta event (has the final usage)
            if (j.contains("type") && j["type"] == "message_delta" &&
                j.contains("usage")) {
                auto &u = j["usage"];
                if (u.contains("input_tokens"))
                    info.prompt_tokens = u["input_tokens"].get<int>();
                if (u.contains("output_tokens"))
                    info.completion_tokens = u["output_tokens"].get<int>();
                if (u.contains("cache_read_input_tokens"))
                    info.cache_read_tokens = u["cache_read_input_tokens"].get<int>();
                if (u.contains("cache_creation_input_tokens"))
                    info.cache_creation_tokens = u["cache_creation_input_tokens"].get<int>();
                info.prompt_tokens += info.cache_read_tokens + info.cache_creation_tokens;
                found_usage = true;
            }
        } catch (const json::parse_error &) {
            // Skip malformed JSON lines silently
        }
    }

    if (!found_usage)
        return std::nullopt;

    info.total_tokens = info.prompt_tokens + info.completion_tokens;
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
            if (u.contains("input_tokens"))
                info.prompt_tokens = u["input_tokens"].get<int>();
            if (u.contains("output_tokens"))
                info.completion_tokens = u["output_tokens"].get<int>();
            if (u.contains("total_tokens"))
                info.total_tokens = u["total_tokens"].get<int>();
            else
                info.total_tokens = info.prompt_tokens + info.completion_tokens;
            // Cached prompt tokens (Responses: usage.input_tokens_details).
            if (u.contains("input_tokens_details") &&
                u["input_tokens_details"].is_object() &&
                u["input_tokens_details"].contains("cached_tokens") &&
                u["input_tokens_details"]["cached_tokens"].is_number_integer())
                info.cache_read_tokens =
                    u["input_tokens_details"]["cached_tokens"].get<int>();
        }
        if (info.total_tokens == 0)
            info.total_tokens = info.prompt_tokens + info.completion_tokens;
        return info;
    } catch (const json::parse_error &e) {
        fprintf(stderr, "[Tracker] Responses JSON parse error: %s\n", e.what());
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
                j["response"].is_object() && j["response"].contains("model"))
                info.model = j["response"]["model"].get<std::string>();
            // Usage lives in response.completed / response.incomplete.
            if ((j.contains("type") && (j["type"] == "response.completed" ||
                                        j["type"] == "response.incomplete")) &&
                j.contains("response") && j["response"].is_object() &&
                j["response"].contains("usage")) {
                auto &u = j["response"]["usage"];
                if (u.contains("input_tokens"))
                    info.prompt_tokens = u["input_tokens"].get<int>();
                if (u.contains("output_tokens"))
                    info.completion_tokens = u["output_tokens"].get<int>();
                if (u.contains("total_tokens"))
                    info.total_tokens = u["total_tokens"].get<int>();
                else
                    info.total_tokens = info.prompt_tokens + info.completion_tokens;
                // Cached prompt tokens (Responses: usage.input_tokens_details).
                if (u.contains("input_tokens_details") &&
                    u["input_tokens_details"].is_object() &&
                    u["input_tokens_details"].contains("cached_tokens") &&
                    u["input_tokens_details"]["cached_tokens"].is_number_integer())
                    info.cache_read_tokens =
                        u["input_tokens_details"]["cached_tokens"].get<int>();
                found_usage = true;
            }
        } catch (const json::parse_error &) {
            // Skip malformed JSON lines silently
        }
    }

    if (!found_usage) return std::nullopt;
    if (info.total_tokens == 0)
        info.total_tokens = info.prompt_tokens + info.completion_tokens;
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

void UsageTracker::log_request(int account_id, int local_key_id,
                               const UsageInfo &usage,
                               bool is_streaming, int status_code,
                               int duration_ms) {
    // Cost is computed automatically by the tr_request_log_insert SQLite
    // trigger, so we pass 0.0 here. The trigger reads model_pricing and
    // sets the correct cost immediately after insert.
    double cost = 0.0;

    db_.log_request(account_id, local_key_id, usage.model,
                    usage.prompt_tokens, usage.completion_tokens,
                    usage.cache_read_tokens, usage.total_tokens, cost,
                    is_streaming, status_code, duration_ms);

    fprintf(stderr,
            "[Tracker] account=%d model=%s prompt=%d comp=%d cache_read=%d "
            "total=%d cost=%.6f stream=%d status=%d dur=%dms\n",
            account_id, usage.model.c_str(),
            usage.prompt_tokens, usage.completion_tokens,
            usage.cache_read_tokens,
            usage.total_tokens, cost,
            is_streaming ? 1 : 0, status_code, duration_ms);
}

// ── log_perf_event ───────────────────────────────────────────────────────

void UsageTracker::log_perf_event(const std::string &model,
                                  int upstream_latency_ms,
                                  int total_latency_ms, int status_code,
                                  bool is_error, int concurrent_count) {
    db_.log_perf_event(model, upstream_latency_ms, total_latency_ms,
                       status_code, is_error, concurrent_count);
}

// ── mark_key_used ────────────────────────────────────────────────────────

void UsageTracker::mark_key_used(int local_key_id) {
    db_.update_key_last_used(local_key_id);
}
