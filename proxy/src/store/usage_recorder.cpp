#include "usage_recorder.h"

#include "core/logging.h"

// The recorder is deliberately storage-only. Protocol JSON/SSE parsing lives
// in format/usage_parser.cpp and the runtime supplies UsageAccounting.
bool UsageRecorder::log_request(
    int account_id, int local_key_id, const UsageAccounting &usage,
    bool is_streaming, int status_code, int duration_ms, int upstream_key_id,
    int ttft_ms, int generation_ms, double output_tps,
    int upstream_ttft_ms, int upstream_duration_ms, int attempt_count,
    const std::vector<Database::AttemptInfo> &attempts, int queue_ms,
    double *out_cost, UsageReservation *reservation) {
    // Cost is computed by the V1 SQLite pricing trigger. Passing zero here
    // preserves the existing writer/database authority boundary.
    double cost = 0.0;
    const bool accepted = db_.log_request(
        account_id, local_key_id, usage.model,
        usage.prompt_tokens, usage.completion_tokens,
        usage.cache_read_tokens, usage.total_tokens, cost, is_streaming,
        status_code, duration_ms, upstream_key_id, ttft_ms, generation_ms,
        output_tps, upstream_ttft_ms, upstream_duration_ms, attempt_count,
        attempts, queue_ms, out_cost, reservation);
    if (!accepted) {
        TB_LOG_ERROR(
            "[Recorder] request log rejected/failed: account=%d model=%s status=%d\n",
            account_id, usage.model.c_str(), status_code);
    }
    return accepted;
}
