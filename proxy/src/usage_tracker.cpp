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
            // for total accuracy when the API provides them
            if (u.contains("cache_read_input_tokens"))
                info.prompt_tokens += u["cache_read_input_tokens"].get<int>();
            if (u.contains("cache_creation_input_tokens"))
                info.prompt_tokens += u["cache_creation_input_tokens"].get<int>();
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
                    info.prompt_tokens += u["cache_read_input_tokens"].get<int>();
                if (u.contains("cache_creation_input_tokens"))
                    info.prompt_tokens += u["cache_creation_input_tokens"].get<int>();
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
                    usage.total_tokens, cost,
                    is_streaming, status_code, duration_ms);

    fprintf(stderr,
            "[Tracker] account=%d model=%s prompt=%d comp=%d total=%d "
            "cost=%.6f stream=%d status=%d dur=%dms\n",
            account_id, usage.model.c_str(),
            usage.prompt_tokens, usage.completion_tokens,
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
