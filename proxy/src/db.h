#pragma once

#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

struct sqlite3;
struct sqlite3_stmt;

/// RAII wrapper around a SQLite3 database connection.
///
/// Opens with WAL mode and foreign keys enabled.  Prepared statements are
/// cached internally for performance; all public methods are thread-safe
/// (guarded by a single mutex — only one query runs at a time per connection).
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
        std::string upstream_key;
        std::string base_url;
        std::string api_format;      // "openai" | "openai_responses" | "anthropic"
        std::string endpoint_path;   // "" = derive from api_format
        std::string auth_header;     // "bearer" | "x-api-key"
        bool is_aggregate = false;   // aggregate account (routes by model)
        std::string account_type;    // "api" | "plan"
        double monthly_price = 0;    // plan monthly price
        int max_concurrency = 0;     // 0 = unlimited
    };
    std::optional<AccountInfo> get_account(int account_id);

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

    void log_request(int account_id, int local_key_id, const std::string &model,
                     int prompt_tokens, int completion_tokens,
                     int cache_read_tokens, int total_tokens,
                     double cost, bool is_streaming, int status_code,
                     int duration_ms);

    void update_key_last_used(int local_key_id);

    // ── Pricing ─────────────────────────────────────────────────────────

    struct PricingEntry {
        int id;
        std::string model_pattern;
        double input_price;
        double output_price;
    };

    std::vector<PricingEntry> get_all_pricing();

    // ── Performance metrics (local-only, not synced) ────────────────────

    void log_perf_event(const std::string &model, int upstream_latency_ms,
                        int total_latency_ms, int status_code,
                        bool is_error, int concurrent_count);

    /// Delete perf events older than `max_age_minutes` (default 24h).
    void cleanup_old_perf_events(int max_age_minutes = 1440);

    // ── In-flight request tracking ──────────────────────────────────────

    /// Record a request that has just started. Returns the row ID to use
    /// in the matching request_end() call.
    int request_start(int local_key_id, int account_id,
                      const std::string &model, bool is_streaming);

    /// Mark a request as completed (delete its in-flight record).
    void request_end(int row_id);

    /// Return the current number of in-flight requests.
    int get_in_flight_count();

    /// Remove in-flight records older than `max_age_minutes` (stuck/crashed).
    void cleanup_stale_in_flight(int max_age_minutes = 10);

private:
    /// Apply pending versioned migrations (PRAGMA user_version-gated) from
    /// `schema_dir`.  Returns false on failure (transaction rolled back).
    bool run_migrations(const std::string &schema_dir);
    void prepare_statements();
    void finalize_statements();

    sqlite3 *db_ = nullptr;
    std::string db_path_;  // used to derive the "<db>.migrate.lock" filename
    std::mutex mutex_;

    // Prepared statements (protected by mutex_)
    sqlite3_stmt *stmt_lookup_key_ = nullptr;
    sqlite3_stmt *stmt_get_account_ = nullptr;
    sqlite3_stmt *stmt_insert_log_ = nullptr;
    sqlite3_stmt *stmt_get_aggregate_entries_ = nullptr;
    sqlite3_stmt *stmt_get_pricing_ = nullptr;
    sqlite3_stmt *stmt_get_timeout_config_ = nullptr;
    sqlite3_stmt *stmt_update_last_used_ = nullptr;
    sqlite3_stmt *stmt_insert_perf_event_ = nullptr;
    sqlite3_stmt *stmt_cleanup_perf_events_ = nullptr;
    sqlite3_stmt *stmt_insert_in_flight_ = nullptr;
    sqlite3_stmt *stmt_delete_in_flight_ = nullptr;
    sqlite3_stmt *stmt_count_in_flight_ = nullptr;
    sqlite3_stmt *stmt_cleanup_in_flight_ = nullptr;
};
