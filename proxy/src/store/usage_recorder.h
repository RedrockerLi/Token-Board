#pragma once

#include "db.h"
#include "usage_accounting.h"

#include <string>

/// Records explicit database-compatible usage and request metadata.
///
/// Protocol JSON/SSE parsing belongs to the format layer. Keeping this class
/// storage-only prevents a second parser from drifting away from codec
/// semantics.
class UsageRecorder {
public:
    explicit UsageRecorder(Database &db) : db_(db) {}

    /// Enqueue a request-log entry using the admission reservation acquired
    /// before upstream access. False means the durable writer rejected it.
    bool log_request(int account_id, int local_key_id,
                     const UsageAccounting &usage,
                     bool is_streaming, int status_code, int duration_ms,
                     int upstream_key_id = 0, int ttft_ms = -1,
                     int generation_ms = -1, double output_tps = -1.0,
                     int upstream_ttft_ms = -1, int upstream_duration_ms = -1,
                     int attempt_count = 1,
                     const std::vector<Database::AttemptInfo> &attempts = {},
                     int queue_ms = 0, double *out_cost = nullptr,
                     UsageReservation *reservation = nullptr);

private:
    Database &db_;
};
