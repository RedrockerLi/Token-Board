#pragma once

#include <optional>
#include <string>

class Database;

/// Extracts OpenAI-compatible usage info from responses and persists them.
///
/// Handles both non-streaming JSON and streaming SSE response formats.
/// Cost is computed automatically by a SQLite trigger on request_log INSERT,
/// so no pricing logic is needed here.
class UsageTracker {
public:
    UsageTracker(Database &db) : db_(db) {}

    struct UsageInfo {
        std::string model;
        int prompt_tokens = 0;
        int completion_tokens = 0;
        int total_tokens = 0;
    };

    /// Parse usage from a non-streaming JSON response body (OpenAI format).
    static std::optional<UsageInfo> parse_usage(const std::string &body);

    /// Parse usage from accumulated SSE streaming data (OpenAI format).
    /// Scans for the last `data:` chunk containing a "usage" object.
    static std::optional<UsageInfo> parse_usage_from_sse(const std::string &sse_data);

    /// Parse usage from a non-streaming Anthropic JSON response.
    /// Anthropic uses usage.input_tokens and usage.output_tokens.
    static std::optional<UsageInfo> parse_anthropic_usage(const std::string &body);

    /// Parse usage from streaming Anthropic SSE data.
    /// Anthropic emits usage in the message_delta event.
    static std::optional<UsageInfo> parse_anthropic_usage_from_sse(const std::string &sse_data);

    /// Compute cost and write a request-log entry.
    void log_request(int account_id, int local_key_id, const UsageInfo &usage,
                     bool is_streaming, int status_code, int duration_ms);

    /// Write a performance-metrics event (local-only, not synced).
    void log_perf_event(const std::string &model, int upstream_latency_ms,
                        int total_latency_ms, int status_code,
                        bool is_error, int concurrent_count);

    /// Update the `last_used_at` timestamp on the local key.
    void mark_key_used(int local_key_id);

private:
    Database &db_;
};
