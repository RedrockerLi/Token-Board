#include "database_internal.h"

UsageReservation::~UsageReservation() {
    Database *db = database_;
    if (!db) return;
    if (upstream_started_) {
        // The request reserved a slot and committed to contacting an upstream,
        // but exited abnormally without completing a UsageEvent.  Record an
        // internal_abort event (zero tokens, sentinel status) instead of
        // silently dropping the slot: log_request consumes this reservation on
        // success, so the capacity is returned exactly once either way.
        bool consumed = false;
        try {
            consumed = db->log_request(
                context_account_id_, context_local_key_id_, context_model_,
                0, 0, 0, 0, 0.0, context_streaming_,
                kInternalAbortStatus, 0, 0, -1, -1, -1.0, -1, -1,
                0, {}, 0, nullptr, this);
        } catch (...) {
            // Never let a destructor throw; the slot is released below.
        }
        if (consumed) return;
    }
    db->release_log_slot();
}

UsageReservation::UsageReservation(UsageReservation &&other) noexcept
    : database_(other.database_) {
    other.database_ = nullptr;
}

UsageReservation &UsageReservation::operator=(UsageReservation &&other) noexcept {
    if (this == &other) return *this;
    if (database_) database_->release_log_slot();
    database_ = other.database_;
    other.database_ = nullptr;
    return *this;
}

std::uint64_t Database::log_spool_bytes() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    return log_spool_write_offset_ - log_spool_read_offset_;
}

std::size_t Database::log_queue_depth() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    return log_memory_queue_.size();
}

std::int64_t Database::log_oldest_age_ms() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    if (log_memory_queue_.empty()) return 0;
    const auto stamp = log_memory_queue_.front().enqueued_at;
    if (stamp.time_since_epoch().count() == 0) return 0;
    return std::max<std::int64_t>(0, std::chrono::duration_cast<
        std::chrono::milliseconds>(std::chrono::steady_clock::now() - stamp).count());
}

bool Database::reserve_log_slot() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    const auto pending_spool = log_spool_write_offset_ - log_spool_read_offset_;
    const auto reserved_frames = log_memory_queue_.size() + log_reservations_ + 1;
    if (!log_accepting_ ||
        reserved_frames > kLogQueueMax ||
        pending_spool + reserved_frames * (kSpoolHeaderBytes + kLogRecordMaxBytes)
            > kLogSpoolHardLimit)
        return false;
    ++log_reservations_;
    return true;
}

std::shared_ptr<UsageReservation> Database::reserve_usage_event() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    const auto pending_spool = log_spool_write_offset_ - log_spool_read_offset_;
    const auto reserved_frames = log_memory_queue_.size() + log_reservations_ + 1;
    if (!log_accepting_ ||
        reserved_frames > kLogQueueMax ||
        pending_spool + reserved_frames * (kSpoolHeaderBytes + kLogRecordMaxBytes)
            > kLogSpoolHardLimit)
        return {};
    ++log_reservations_;
    return std::shared_ptr<UsageReservation>(new UsageReservation(this));
}

void Database::release_log_slot() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    if (log_reservations_ > 0) --log_reservations_;
}

bool Database::log_writer_healthy() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    return log_accepting_;
}

bool Database::log_recovery_complete() {
    std::lock_guard<std::mutex> lock(log_queue_mutex_);
    return !log_recovering_ && log_accepting_;
}

void Database::set_cost_observer(CostObserver observer) {
    std::lock_guard<std::mutex> lock(cost_observer_mutex_);
    cost_observer_ = std::move(observer);
}

bool Database::persist_log_records(const LogRecord *records,
                                   std::size_t count) {
    if (count == 0) return true;
    std::lock_guard<std::mutex> lock(write_mutex_);
    if (!write_db_) return false;

    // A failed rollback can leave the connection inside a transaction. Retry a
    // cleanup before BEGIN; event_id makes every later replay idempotent.
    if (sqlite3_get_autocommit(write_db_) == 0 &&
        sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr) !=
            SQLITE_OK) {
        TB_LOG_ERROR( "[DB] request-log pre-BEGIN ROLLBACK error: %s\n",
                sqlite3_errmsg(write_db_));
        return false;
    }

    int rc = sqlite3_exec(write_db_, "BEGIN IMMEDIATE", nullptr, nullptr, nullptr);
    if (rc != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] request-log BEGIN error (%d): %s\n", rc,
                sqlite3_errmsg(write_db_));
        return false;
    }
    std::vector<const LogRecord *> inserted_records;
    inserted_records.reserve(count);
    for (std::size_t i = 0; i < count; ++i) {
        bool inserted = false;
        if (!write_log_record_in_transaction(records[i], &inserted)) {
            if (sqlite3_get_autocommit(write_db_) == 0 &&
                sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr) !=
                    SQLITE_OK)
                TB_LOG_ERROR( "[DB] request-log ROLLBACK error: %s\n",
                        sqlite3_errmsg(write_db_));
            return false;
        }
        if (inserted) inserted_records.push_back(&records[i]);
    }
    rc = sqlite3_exec(write_db_, "COMMIT", nullptr, nullptr, nullptr);
    if (rc == SQLITE_OK) {
        // Accounting latency is measured through the successful commit, not
        // merely the INSERT step.  Update the committed rows after COMMIT;
        // if this tiny follow-up fails the durable spool stays replayable and
        // the next pass retries the metric update by event_id.
        if (!update_accounting_metrics(
                [&] {
                    std::vector<const LogRecord *> all;
                    all.reserve(count);
                    for (std::size_t i = 0; i < count; ++i)
                        all.push_back(&records[i]);
                    return all;
                }()))
            return false;
        // V1 pricing is authoritative in SQLite. Notify the process-local
        // routing ledger only after the transaction is durable, so a failed
        // batch cannot bias routing with a cost that was never recorded.
        notify_rated_costs(inserted_records);
        return true;
    }
    TB_LOG_ERROR( "[DB] request-log COMMIT error (%d): %s\n", rc,
            sqlite3_errmsg(write_db_));
    if (sqlite3_get_autocommit(write_db_) == 0 &&
        sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr) !=
            SQLITE_OK)
        TB_LOG_ERROR( "[DB] request-log ROLLBACK-after-COMMIT error: %s\n",
                sqlite3_errmsg(write_db_));
    // The transaction may already have committed. Replaying is safe because
    // event_id is unique and checked before inserting attempts.
    return false;
}

void Database::notify_rated_costs(
    const std::vector<const LogRecord *> &records) {
    CostObserver observer;
    {
        std::lock_guard<std::mutex> lock(cost_observer_mutex_);
        observer = cost_observer_;
    }
    if (!observer || !write_db_) return;

    sqlite3_stmt *statement = nullptr;
    const char *sql = "SELECT equivalent_cost FROM request_log "
                      "WHERE event_id=?1";
    if (sqlite3_prepare_v2(write_db_, sql, -1, &statement, nullptr) != SQLITE_OK)
        return;
    for (const LogRecord *record : records) {
        if (!record || record->upstream_key_id == 0) continue;
        sqlite3_reset(statement);
        sqlite3_bind_text(statement, 1, record->event_id.c_str(), -1,
                          SQLITE_STATIC);
        if (sqlite3_step(statement) == SQLITE_ROW) {
            const double cost = sqlite3_column_double(statement, 0);
            if (cost > 0.0) observer(record->upstream_key_id, cost);
        }
    }
    sqlite3_finalize(statement);
}

bool Database::write_log_record_in_transaction(const LogRecord &record,
                                               bool *inserted) {
    if (inserted) *inserted = false;
    sqlite3_reset(stmt_find_log_event_);
    sqlite3_bind_text(stmt_find_log_event_, 1, record.event_id.c_str(),
                      static_cast<int>(record.event_id.size()), SQLITE_STATIC);
    int rc = sqlite3_step(stmt_find_log_event_);
    if (rc == SQLITE_ROW) {
        sqlite3_reset(stmt_find_log_event_);
        return true;
    }
    sqlite3_reset(stmt_find_log_event_);
    if (rc != SQLITE_DONE) {
        TB_LOG_ERROR( "[DB] request-log event lookup error (%d): %s\n", rc,
                sqlite3_errmsg(write_db_));
        return false;
    }
    if (inserted) *inserted = true;

    sqlite3_reset(stmt_insert_log_);
    sqlite3_bind_int(stmt_insert_log_, 1, record.account_id);
    sqlite3_bind_int(stmt_insert_log_, 2, record.local_key_id);
    sqlite3_bind_text(stmt_insert_log_, 3, record.model.c_str(),
                      static_cast<int>(record.model.size()), SQLITE_STATIC);
    sqlite3_bind_int(stmt_insert_log_, 4, record.prompt_tokens);
    sqlite3_bind_int(stmt_insert_log_, 5, record.completion_tokens);
    sqlite3_bind_int(stmt_insert_log_, 6, record.cache_read_tokens);
    sqlite3_bind_int(stmt_insert_log_, 7, record.total_tokens);
    sqlite3_bind_double(stmt_insert_log_, 8, record.cost);
    sqlite3_bind_int(stmt_insert_log_, 9, record.is_streaming ? 1 : 0);
    sqlite3_bind_int(stmt_insert_log_, 10, record.status_code);
    sqlite3_bind_int(stmt_insert_log_, 11, record.duration_ms);
    sqlite3_bind_int(stmt_insert_log_, 12, record.upstream_key_id);
    if (record.ttft_ms >= 0)
        sqlite3_bind_int(stmt_insert_log_, 13, record.ttft_ms);
    else
        sqlite3_bind_null(stmt_insert_log_, 13);
    if (record.generation_ms >= 0)
        sqlite3_bind_int(stmt_insert_log_, 14, record.generation_ms);
    else
        sqlite3_bind_null(stmt_insert_log_, 14);
    if (record.output_tps >= 0.0)
        sqlite3_bind_double(stmt_insert_log_, 15, record.output_tps);
    else
        sqlite3_bind_null(stmt_insert_log_, 15);
    if (record.upstream_ttft_ms >= 0)
        sqlite3_bind_int(stmt_insert_log_, 16, record.upstream_ttft_ms);
    else
        sqlite3_bind_null(stmt_insert_log_, 16);
    if (record.upstream_duration_ms >= 0)
        sqlite3_bind_int(stmt_insert_log_, 17, record.upstream_duration_ms);
    else
        sqlite3_bind_null(stmt_insert_log_, 17);
    sqlite3_bind_int(stmt_insert_log_, 18,
                     std::max(0, record.attempt_count));
    sqlite3_bind_int(stmt_insert_log_, 19,
                     std::max(0, record.attempt_count - 1));
    sqlite3_bind_int64(stmt_insert_log_, 20, record.requested_at_unix);
    sqlite3_bind_text(stmt_insert_log_, 21, record.event_id.c_str(),
                      static_cast<int>(record.event_id.size()), SQLITE_STATIC);
    sqlite3_bind_int(stmt_insert_log_, 22, record.queue_ms);
    // Filled after the transaction commits by update_accounting_metrics.
    sqlite3_bind_int(stmt_insert_log_, 23, 0);

    rc = sqlite3_step(stmt_insert_log_);
    sqlite3_reset(stmt_insert_log_);
    if (rc != SQLITE_DONE) {
        TB_LOG_ERROR(
                "[DB] request_log INSERT error (%d): %s "
                "(account=%d local_key=%d status=%d)\n",
                rc, sqlite3_errmsg(write_db_), record.account_id,
                record.local_key_id, record.status_code);
        return false;
    }

    const sqlite3_int64 request_log_id = sqlite3_last_insert_rowid(write_db_);
    for (std::size_t i = 0; i < record.attempts.size(); ++i) {
        const auto &attempt = record.attempts[i];
        sqlite3_reset(stmt_insert_attempt_);
        sqlite3_bind_int64(stmt_insert_attempt_, 1, request_log_id);
        sqlite3_bind_int(stmt_insert_attempt_, 2, static_cast<int>(i + 1));
        sqlite3_bind_int(stmt_insert_attempt_, 3, attempt.upstream_id);
        sqlite3_bind_int(stmt_insert_attempt_, 17, attempt.account_id);
        if (attempt.upstream_key_id != 0)
            sqlite3_bind_int(stmt_insert_attempt_, 4, attempt.upstream_key_id);
        else
            sqlite3_bind_null(stmt_insert_attempt_, 4);
        sqlite3_bind_int(stmt_insert_attempt_, 5, attempt.status_code);
        sqlite3_bind_int(stmt_insert_attempt_, 6, attempt.duration_ms);
        if (attempt.ttft_ms >= 0)
            sqlite3_bind_int(stmt_insert_attempt_, 7, attempt.ttft_ms);
        else
            sqlite3_bind_null(stmt_insert_attempt_, 7);
        sqlite3_bind_int(stmt_insert_attempt_, 8,
                         attempt.is_timeout ? 1 : 0);
        sqlite3_bind_text(stmt_insert_attempt_, 9, attempt.error.c_str(),
                          static_cast<int>(attempt.error.size()),
                          SQLITE_TRANSIENT);
        sqlite3_bind_int64(stmt_insert_attempt_, 10,
                           record.requested_at_unix);
        sqlite3_bind_int(stmt_insert_attempt_, 11, attempt.dns_ms);
        sqlite3_bind_int(stmt_insert_attempt_, 12, attempt.connect_ms);
        sqlite3_bind_int(stmt_insert_attempt_, 13, attempt.tls_ms);
        sqlite3_bind_int(stmt_insert_attempt_, 14, attempt.lease_wait_ms);
        sqlite3_bind_int(stmt_insert_attempt_, 15, attempt.first_byte_ms);
        sqlite3_bind_int(stmt_insert_attempt_, 16,
                         attempt.connection_reused ? 1 : 0);
        rc = sqlite3_step(stmt_insert_attempt_);
        sqlite3_reset(stmt_insert_attempt_);
        if (rc != SQLITE_DONE) {
            TB_LOG_ERROR(
                    "[DB] request_attempt INSERT error (%d): %s "
                    "(request=%lld attempt=%zu)\n",
                    rc, sqlite3_errmsg(write_db_),
                    static_cast<long long>(request_log_id), i + 1);
            return false;
        }
    }

    sqlite3_reset(stmt_update_last_used_);
    sqlite3_bind_int(stmt_update_last_used_, 1, record.local_key_id);
    sqlite3_bind_int64(stmt_update_last_used_, 2, record.requested_at_unix);
    rc = sqlite3_step(stmt_update_last_used_);
    sqlite3_reset(stmt_update_last_used_);
    if (rc != SQLITE_DONE) {
        TB_LOG_ERROR( "[DB] request-log last_used UPDATE error (%d): %s\n",
                rc, sqlite3_errmsg(write_db_));
        return false;
    }
    return true;
}

bool Database::update_accounting_metrics(
    const std::vector<const LogRecord *> &records) {
    for (const LogRecord *record : records) {
        if (!record) continue;
        const int accounting_ms = record->enqueued_at.time_since_epoch().count()
            ? static_cast<int>(std::max<std::int64_t>(0,
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() -
                    record->enqueued_at).count()))
            : 0;
        sqlite3_reset(stmt_update_accounting_);
        sqlite3_bind_int(stmt_update_accounting_, 1, accounting_ms);
        sqlite3_bind_text(stmt_update_accounting_, 2, record->event_id.c_str(),
                          static_cast<int>(record->event_id.size()), SQLITE_STATIC);
        const int rc = sqlite3_step(stmt_update_accounting_);
        sqlite3_reset(stmt_update_accounting_);
        if (rc != SQLITE_DONE) {
            TB_LOG_ERROR("[DB] request-log accounting metric update error (%d): %s\n",
                         rc, sqlite3_errmsg(write_db_));
            log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        log_last_accounting_ms_.store(static_cast<std::uint64_t>(accounting_ms),
                                      std::memory_order_release);
    }
    return true;
}

bool Database::log_request(int account_id, int local_key_id,
                           const std::string &model,
                           int prompt_tokens, int completion_tokens,
                           int cache_read_tokens, int total_tokens,
                           double cost,
                           bool is_streaming, int status_code,
                           int duration_ms, int upstream_key_id,
                           int ttft_ms, int generation_ms, double output_tps,
                           int upstream_ttft_ms, int upstream_duration_ms,
                           int attempt_count,
                           const std::vector<AttemptInfo> &attempts,
                           int queue_ms,
                           double *out_cost,
                           UsageReservation *reservation) {
    // Hold the admission slot until the fully-owned event is in the writer
    // queue.  A failed allocation or a writer shutdown therefore leaves the
    // RAII token available for its caller to release, instead of silently
    // losing capacity or an accepted billable request.
    bool reserved = false;
    {
        std::lock_guard<std::mutex> lock(log_queue_mutex_);
        reserved = log_reservations_ > 0 && reservation != nullptr &&
                   reservation->belongs_to(this);
    }
    if (!reserved) {
        // Every accepted usage request must have claimed capacity before it
        // touched the upstream.  Allowing an unreserved direct write would
        // make queue saturation racy and could accept a billable request that
        // has no durable accounting slot.
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        return false;
    }
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    LogRecord record;
    try {
        record.account_id = account_id;
        record.local_key_id = local_key_id;
        record.model = bounded_string(model, kLogModelMaxBytes);
        record.prompt_tokens = std::max(0, prompt_tokens);
        record.completion_tokens = std::max(0, completion_tokens);
        record.cache_read_tokens = std::max(0, cache_read_tokens);
        record.total_tokens = std::max(0, total_tokens);
        record.cost = std::isfinite(cost) ? cost : 0.0;
        record.is_streaming = is_streaming;
        record.status_code = status_code;
        record.duration_ms = duration_ms;
        record.upstream_key_id = upstream_key_id;
        record.ttft_ms = ttft_ms;
        record.generation_ms = generation_ms;
        record.output_tps = output_tps;
        record.upstream_ttft_ms = upstream_ttft_ms;
        record.upstream_duration_ms = upstream_duration_ms;
        record.attempt_count = std::max(0, attempt_count);
        record.queue_ms = std::max(0, queue_ms);
        record.enqueued_at = std::chrono::steady_clock::now();
        const std::size_t retained = std::min(attempts.size(), kLogAttemptsMax);
        record.attempts.reserve(retained);
        for (std::size_t i = 0; i < retained; ++i) {
            // Preserve the final outcome when an unusually large fallback chain
            // must be bounded for spool/memory safety.
            const std::size_t source =
                attempts.size() > kLogAttemptsMax && i + 1 == retained
                    ? attempts.size() - 1 : i;
            AttemptInfo bounded = attempts[source];
            bounded.error = bounded_string(bounded.error, kLogErrorMaxBytes);
            record.attempts.push_back(std::move(bounded));
        }
        record.requested_at_unix =
            std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
        if (attempts.size() > kLogAttemptsMax) {
            TB_LOG_ERROR(
                    "[DB] request-log attempts truncated from %zu to %zu\n",
                    attempts.size(), kLogAttemptsMax);
        }

        // V1.1+ prices every proxy/import UsageEvent in SQLite using
        // requested_at. A zero value asks that authoritative path to resolve
        // historical rate/slot/FX data.
        record.cost = 0.0;
        std::array<unsigned char, 16> random_bytes {};
        sqlite3_randomness(static_cast<int>(random_bytes.size()),
                           random_bytes.data());
        static constexpr char hex[] = "0123456789abcdef";
        record.event_id.resize(random_bytes.size() * 2);
        for (std::size_t i = 0; i < random_bytes.size(); ++i) {
            record.event_id[i * 2] = hex[random_bytes[i] >> 4];
            record.event_id[i * 2 + 1] = hex[random_bytes[i] & 0x0f];
        }
    } catch (const std::exception &e) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
        TB_LOG_ERROR( "[DB] request-log record allocation error: %s\n",
                e.what());
        return false;
    } catch (...) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
        TB_LOG_ERROR( "[DB] request-log record allocation error: unknown exception\n");
        return false;
    }

    const double accepted_cost = record.cost;
    {
        std::lock_guard<std::mutex> lock(log_queue_mutex_);
        if (!log_accepting_) {
            log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        --log_reservations_;
        if (reservation) reservation->consume();
        log_memory_queue_.push_back(std::move(record));
    }
    log_queue_cv_.notify_one();
    // The authoritative equivalent cost is resolved by SQLite after the
    // pending UsageEvent is inserted.  The post-commit observer reads that
    // value before updating any process-local ledger.
    if (out_cost) *out_cost = accepted_cost;
    return true;
}

// ── resolve_aggregate ────────────────────────────────────────────────────
