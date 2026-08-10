#include "database_internal.h"

std::string Database::serialize_log_record(const LogRecord &record) {
    json encoded_attempts = json::array();
    for (const auto &attempt : record.attempts) {
        encoded_attempts.push_back({
            {"account_id", attempt.account_id},
            {"upstream_id", attempt.upstream_id},
            {"upstream_key_id", attempt.upstream_key_id},
            {"status_code", attempt.status_code},
            {"duration_ms", attempt.duration_ms},
            {"dns_ms", attempt.dns_ms},
            {"connect_ms", attempt.connect_ms},
            {"tls_ms", attempt.tls_ms},
            {"lease_wait_ms", attempt.lease_wait_ms},
            {"first_byte_ms", attempt.first_byte_ms},
            {"connection_reused", attempt.connection_reused},
            {"ttft_ms", attempt.ttft_ms},
            {"is_timeout", attempt.is_timeout},
            {"error", attempt.error},
        });
    }
    json value = {
        {"v", 1},
        {"event_id", record.event_id},
        {"account_id", record.account_id},
        {"local_key_id", record.local_key_id},
        {"model", record.model},
        {"prompt_tokens", record.prompt_tokens},
        {"completion_tokens", record.completion_tokens},
        {"cache_read_tokens", record.cache_read_tokens},
        {"total_tokens", record.total_tokens},
        {"cost", record.cost},
        {"is_streaming", record.is_streaming},
        {"status_code", record.status_code},
        {"duration_ms", record.duration_ms},
        {"queue_ms", record.queue_ms},
        {"upstream_key_id", record.upstream_key_id},
        {"ttft_ms", record.ttft_ms},
        {"generation_ms", record.generation_ms},
        {"output_tps", record.output_tps},
        {"upstream_ttft_ms", record.upstream_ttft_ms},
        {"upstream_duration_ms", record.upstream_duration_ms},
        {"attempt_count", record.attempt_count},
        {"requested_at_unix", record.requested_at_unix},
        {"attempts", std::move(encoded_attempts)},
    };
    return value.dump(-1, ' ', false, json::error_handler_t::replace);
}

bool Database::deserialize_log_record(const std::string &payload,
                                      LogRecord &record) {
    try {
        const auto value = json::parse(payload);
        if (!value.is_object() || value.value("v", 0) != 1) return false;
        record.event_id = value.value("event_id", std::string());
        if (record.event_id.empty() || record.event_id.size() > 128) return false;
        record.account_id = value.value("account_id", 0);
        record.local_key_id = value.value("local_key_id", 0);
        record.model = bounded_string(value.value("model", std::string()),
                                      kLogModelMaxBytes);
        record.prompt_tokens = value.value("prompt_tokens", 0);
        record.completion_tokens = value.value("completion_tokens", 0);
        record.cache_read_tokens = value.value("cache_read_tokens", 0);
        record.total_tokens = value.value("total_tokens", 0);
        record.cost = value.value("cost", 0.0);
        record.is_streaming = value.value("is_streaming", false);
        record.status_code = value.value("status_code", 0);
        record.duration_ms = value.value("duration_ms", 0);
        record.queue_ms = std::max(0, value.value("queue_ms", 0));
        record.enqueued_at = std::chrono::steady_clock::now();
        record.upstream_key_id = value.value("upstream_key_id", 0);
        record.ttft_ms = value.value("ttft_ms", -1);
        record.generation_ms = value.value("generation_ms", -1);
        record.output_tps = value.value("output_tps", -1.0);
        record.upstream_ttft_ms = value.value("upstream_ttft_ms", -1);
        record.upstream_duration_ms = value.value("upstream_duration_ms", -1);
        record.attempt_count = std::max(0, value.value("attempt_count", 0));
        record.requested_at_unix = value.value<std::int64_t>(
            "requested_at_unix", 0);
        if (!std::isfinite(record.cost)) return false;
        if (!std::isfinite(record.output_tps)) record.output_tps = -1.0;

        record.attempts.clear();
        const auto it = value.find("attempts");
        if (it != value.end() && it->is_array()) {
            record.attempts.reserve(std::min(it->size(), kLogAttemptsMax));
            for (const auto &encoded : *it) {
                if (record.attempts.size() >= kLogAttemptsMax) break;
                AttemptInfo attempt;
                attempt.account_id = encoded.value("account_id", 0);
                attempt.upstream_id = encoded.value("upstream_id", 0);
                attempt.upstream_key_id = encoded.value("upstream_key_id", 0);
                attempt.status_code = encoded.value("status_code", 0);
                attempt.duration_ms = encoded.value("duration_ms", 0);
                attempt.dns_ms = encoded.value("dns_ms", 0);
                attempt.connect_ms = encoded.value("connect_ms", 0);
                attempt.tls_ms = encoded.value("tls_ms", 0);
                attempt.lease_wait_ms = encoded.value("lease_wait_ms", 0);
                attempt.first_byte_ms = encoded.value("first_byte_ms", 0);
                attempt.connection_reused = encoded.value("connection_reused", false);
                attempt.ttft_ms = encoded.value("ttft_ms", -1);
                attempt.is_timeout = encoded.value("is_timeout", false);
                attempt.error = bounded_string(
                    encoded.value("error", std::string()), kLogErrorMaxBytes);
                record.attempts.push_back(std::move(attempt));
            }
        }
        return true;
    } catch (const std::exception &) {
        return false;
    }
}
