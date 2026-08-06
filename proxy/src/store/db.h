#pragma once

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

struct sqlite3;
struct sqlite3_stmt;

/// RAII wrapper around a SQLite3 database connection.
///
/// Opens with WAL mode and foreign keys enabled.  Prepared statements are
/// cached internally for performance; all public methods are thread-safe.
/// Read and write statements use separate SQLite connections so a writer
/// waiting on WAL's busy timeout cannot convoy unrelated route lookups.
class Database {
public:
    Database() = default;
    ~Database();

    // Non-copyable, movable
    Database(const Database &) = delete;
    Database &operator=(const Database &) = delete;
    Database(Database &&) = delete;
    Database &operator=(Database &&) = delete;

    /// Open (or create) the database at `path`.  Applies pending schema
    /// migrations from `schema_dir` (schema/<db>/NNNN_*.sql).  Returns true
    /// on success.
    bool open(const std::string &path, const std::string &schema_dir);

    /// Close the database and finalize all prepared statements.
    void close();

    // ── Lookups ──────────────────────────────────────────────────────────

    struct KeyInfo {
        int id;
        std::string key_value;
        int account_id;
        std::string label;
    };
    std::optional<KeyInfo> lookup_local_key(const std::string &key_value);

    struct AccountInfo {
        int id;
        std::string name;
        std::string base_url;
        std::string api_format;      // "openai" | "openai_responses" | "anthropic"
        std::string endpoint_path;   // "" = derive from api_format
        std::string auth_header;     // "bearer" | "x-api-key"
        bool is_aggregate = false;   // aggregate account (routes by model)
        std::string account_type;    // spec identity: "api" | "plan" | "agent"
                                     // (semantics in app/domain/account_types.py
                                     // + proxy/src/core/account_types.h)
        double monthly_price = 0;    // plan monthly price
        int max_concurrency = 0;     // 0 = unlimited
        bool deleted = false;        // soft-deleted with a PAST deleted_at
                                     // (a future deleted_at = end_of_period
                                     // cancellation, still routable until then)
    };
    std::optional<AccountInfo> get_account(int account_id);

    struct RouteInfo {
        KeyInfo key;
        AccountInfo account;
    };
    /// Authenticate a local key and read its account in one snapshot/query.
    std::optional<RouteInfo> lookup_route(const std::string &key_value);

    // ── Upstream keys (multi-key per account, local-only) ───────────────

    /// One API key slot of an account (child of upstream_accounts).  The key
    /// itself is a local secret — never synced to the cloud.
    struct KeySlot {
        int id;
        std::string key_value;
        int position = 0;   // fill / session-affinity preference order
    };
    /// All keys of an account, ordered by (position, id).
    std::vector<KeySlot> get_upstream_keys(int account_id);

    /// One fully resolved real-upstream target from a routing snapshot.
    /// `keys` and `account` are read by the same SQLite statement, so callers
    /// cannot combine credentials from one config revision with an endpoint
    /// from another.  `keys` holds the per-key slots (the only key source since
    /// the legacy single-column upstream_key was removed).
    struct RoutingTarget {
        AccountInfo account;
        std::vector<KeySlot> keys;
        std::string upstream_model;
        int priority_group = 0;
    };
    /// Resolve a plain or aggregate account for `model` in one consistent
    /// read snapshot.  Aggregate targets are ordered by (sort_order, id), and
    /// missing or soft-deleted accounts are omitted.
    std::vector<RoutingTarget> resolve_routing_snapshot(
        int account_id, const std::string &model);

    // ── Timeout config (per client wire format) ────────────────────────

    /// Per-wire-format upstream timeouts (seconds; 0 = disabled).
    struct TimeoutConfig {
        int streaming_first_byte_timeout = 60;  // wait for first streaming chunk
        int streaming_idle_timeout = 120;       // max gap between chunks; 0 = disabled
        int non_streaming_timeout = 600;        // non-streaming body read timeout
    };
    /// Timeout config for a client wire format: "anthropic" | "openai_responses"
    /// | "openai" (any other value → defaults).  Falls back to the struct
    /// defaults when the row is missing.
    TimeoutConfig get_timeout_config(const std::string &app_type);

    // ── Aggregate accounts ─────────────────────────────────────────────

    /// One model-mapping entry of an aggregate account.
    struct AggregateEntry {
        std::string pattern;          // glob, matched against the request model
        int upstream_account_id = 0;  // real upstream account
        std::string upstream_model;   // model name forwarded upstream
    };
    /// Resolve the real upstream targets for `model` on an aggregate account.
    /// Returns ALL matching entries in priority order (sort_order, id) — one
    /// model may map to several upstream accounts; the caller tries them in
    /// order (skipping full / cooling-down accounts).
    std::vector<AggregateEntry> resolve_aggregate(int account_id,
                                                  const std::string &model);
    /// All model patterns of an aggregate account (used by /v1/models).
    std::vector<std::string> get_aggregate_model_patterns(int account_id);

    // ── Usage logging ───────────────────────────────────────────────────

    struct AttemptInfo {
        int account_id = 0;
        int upstream_key_id = 0;
        int status_code = 0;
        int duration_ms = 0;
        int ttft_ms = -1;          // semantic TTFT; -1 when not observed
        bool is_timeout = false;
        std::string error;
    };

    /// Durably append one request and all of its attempts to the local spool.
    /// A dedicated writer replays the spool into SQLite atomically. Request
    /// threads never fall back to SQLite when the writer is busy; false means
    /// the spool append failed or shutdown has stopped accepting new records.
    bool log_request(int account_id, int local_key_id, const std::string &model,
                     int prompt_tokens, int completion_tokens,
                     int cache_read_tokens, int total_tokens,
                     double cost, bool is_streaming, int status_code,
                     int duration_ms, int upstream_key_id = 0,
                     int ttft_ms = -1, int generation_ms = -1,
                     double output_tps = -1.0,
                     int upstream_ttft_ms = -1, int upstream_duration_ms = -1,
                     int attempt_count = 1,
                     const std::vector<AttemptInfo> &attempts = {},
                     double *out_cost = nullptr);

    // ── Pricing ─────────────────────────────────────────────────────────

    struct PricingEntry {
        int id;
        std::string model_pattern;
        double input_price;
        double output_price;
    };

    std::vector<PricingEntry> get_all_pricing();

private:
    struct LogRecord {
        int account_id = 0;
        int local_key_id = 0;
        std::string model;
        int prompt_tokens = 0;
        int completion_tokens = 0;
        int cache_read_tokens = 0;
        int total_tokens = 0;
        double cost = 0.0;
        bool is_streaming = false;
        int status_code = 0;
        int duration_ms = 0;
        int upstream_key_id = 0;
        int ttft_ms = -1;
        int generation_ms = -1;
        double output_tps = -1.0;
        int upstream_ttft_ms = -1;
        int upstream_duration_ms = -1;
        int attempt_count = 1;
        std::vector<AttemptInfo> attempts;
        std::int64_t requested_at_unix = 0;
        std::string event_id;
        bool cost_frozen = false;
    };

    struct SpoolRecord {
        LogRecord record;
        std::uint64_t end_offset = 0;
        std::size_t frame_bytes = 0;
    };

    struct FrozenRate {
        bool matched = false;
        double input_price = 0.0;
        double output_price = 0.0;
        double cache_read_price = 0.0;
        double multiplier = 1.0;
        std::string currency = "CNY";
        double fx_rate = 1.0;  // USD→CNY; 1.0 for CNY rows or when no rate stored
    };

    /// Apply pending versioned migrations (PRAGMA user_version-gated) from
    /// `schema_dir`.  Returns false on failure (transaction rolled back).
    bool run_migrations(const std::string &schema_dir);
    bool prepare_statements();
    void finalize_statements();

    bool start_log_writer();
    void stop_log_writer();
    void log_writer_loop();
    bool persist_log_records(const LogRecord *records, std::size_t count);
    bool write_log_record_in_transaction(const LogRecord &record);
    bool snapshot_request_cost(const std::string &model, int prompt_tokens,
                               int completion_tokens, int cache_read_tokens,
                               std::int64_t requested_at_unix,
                               double &cost);
    bool append_log_spool_locked(const std::string &payload);
    bool read_log_spool_batch_locked(std::vector<SpoolRecord> &batch);
    bool compact_log_spool_locked(bool force);
    static std::string serialize_log_record(const LogRecord &record);
    static bool deserialize_log_record(const std::string &payload,
                                       LogRecord &record);

    static constexpr std::size_t kLogBatchSize = 64;
    static constexpr std::size_t kLogBatchBytes = 1024 * 1024;
    static constexpr std::size_t kLogRecordMaxBytes = 256 * 1024;
    static constexpr std::size_t kLogModelMaxBytes = 512;
    static constexpr std::size_t kLogErrorMaxBytes = 2048;
    static constexpr std::size_t kLogAttemptsMax = 64;
    static constexpr std::uint64_t kLogCompactThreshold = 8 * 1024 * 1024;
    // Hard cap on the durable request-log spool file.  Appends past this are
    // rejected so a wedged/unavailable writer can never grow the spool without
    // bound on disk.  Not a data-loss-free guard: once hit, new records drop
    // until the writer drains (log_accepting_ is the first line of defense).
    static constexpr std::uint64_t kLogSpoolHardLimit = 256 * 1024 * 1024;
    static constexpr int kShutdownRetryLimit = 3;

    sqlite3 *write_db_ = nullptr;
    sqlite3 *read_db_ = nullptr;
    sqlite3 *pricing_db_ = nullptr;
    std::string db_path_;  // used to derive the "<db>.migrate.lock" filename
    mutable std::shared_mutex lifecycle_mutex_;
    std::mutex write_mutex_;
    std::mutex read_mutex_;
    std::mutex pricing_mutex_;

    // The append-only spool is the durable queue. Request threads only serialize
    // and fdatasync a bounded record; SQLite I/O belongs to log_writer_thread_.
    std::mutex log_queue_mutex_;
    std::condition_variable log_queue_cv_;
    std::thread log_writer_thread_;
    bool log_accepting_ = false;
    bool log_stop_ = false;
    int log_spool_fd_ = -1;
    std::uint64_t log_spool_read_offset_ = 0;
    std::uint64_t log_spool_write_offset_ = 0;
    std::atomic<std::uint64_t> log_persist_failures_{0};
    std::unordered_map<std::string, FrozenRate> frozen_rate_cache_;

    // Read statements (read_db_, protected by read_mutex_)
    sqlite3_stmt *stmt_lookup_key_ = nullptr;
    sqlite3_stmt *stmt_get_account_ = nullptr;
    sqlite3_stmt *stmt_lookup_route_ = nullptr;
    sqlite3_stmt *stmt_get_upstream_keys_ = nullptr;
    sqlite3_stmt *stmt_resolve_routing_snapshot_ = nullptr;
    sqlite3_stmt *stmt_get_aggregate_entries_ = nullptr;
    sqlite3_stmt *stmt_get_pricing_ = nullptr;
    sqlite3_stmt *stmt_snapshot_price_ = nullptr;
    sqlite3_stmt *stmt_get_timeout_config_ = nullptr;

    // Mutating statements (write_db_, protected by write_mutex_)
    sqlite3_stmt *stmt_insert_log_ = nullptr;
    sqlite3_stmt *stmt_find_log_event_ = nullptr;
    sqlite3_stmt *stmt_insert_attempt_ = nullptr;
    sqlite3_stmt *stmt_update_last_used_ = nullptr;
};
