#pragma once

#include <optional>
#include <string>

class Database;
class ModelPricing;

/// Extracts OpenAI-compatible usage info from responses and persists them.
///
/// Handles both non-streaming JSON and streaming SSE response formats.
class UsageTracker {
public:
    UsageTracker(Database &db, ModelPricing &pricing)
        : db_(db), pricing_(pricing) {}

    struct UsageInfo {
        std::string model;
        int prompt_tokens = 0;
        int completion_tokens = 0;
        int total_tokens = 0;
    };

    /// Parse usage from a non-streaming JSON response body.
    static std::optional<UsageInfo> parse_usage(const std::string &body);

    /// Parse usage from accumulated SSE streaming data.
    /// Scans for the last `data:` chunk containing a "usage" object.
    static std::optional<UsageInfo> parse_usage_from_sse(const std::string &sse_data);

    /// Compute cost and write a request-log entry.
    void log_request(int account_id, int local_key_id, const UsageInfo &usage,
                     bool is_streaming, int status_code, int duration_ms);

    /// Update the `last_used_at` timestamp on the local key.
    void mark_key_used(int local_key_id);

private:
    Database &db_;
    ModelPricing &pricing_;
};
