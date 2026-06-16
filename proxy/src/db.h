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

    /// Open (or create) the database at `path`.  Creates all tables on first
    /// open.  Returns true on success.
    bool open(const std::string &path);

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
    };
    std::optional<AccountInfo> get_account(int account_id);

    // ── Usage logging ───────────────────────────────────────────────────

    void log_request(int account_id, int local_key_id, const std::string &model,
                     int prompt_tokens, int completion_tokens, int total_tokens,
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
    struct ModelMapping {
        std::string pattern;
        std::string upstream_model;
    };
    int get_key_template_id(int key_id);
    std::vector<ModelMapping> get_template_entries(int template_id);
    std::vector<ModelMapping> get_key_model_mappings(int key_id);

    std::vector<PricingEntry> get_all_pricing();

    // ── Performance metrics (local-only, not synced) ────────────────────

    void log_perf_event(const std::string &model, int upstream_latency_ms,
                        int total_latency_ms, int status_code,
                        bool is_error, int concurrent_count);

    /// Delete perf events older than `max_age_minutes` (default 24h).
    void cleanup_old_perf_events(int max_age_minutes = 1440);

private:
    void create_schema();
    void prepare_statements();
    void finalize_statements();

    sqlite3 *db_ = nullptr;
    std::mutex mutex_;

    // Prepared statements (protected by mutex_)
    sqlite3_stmt *stmt_lookup_key_ = nullptr;
    sqlite3_stmt *stmt_get_account_ = nullptr;
    sqlite3_stmt *stmt_insert_log_ = nullptr;
    sqlite3_stmt *stmt_get_key_mappings_ = nullptr;
    sqlite3_stmt *stmt_get_key_template_ = nullptr;
    sqlite3_stmt *stmt_get_template_entries_ = nullptr;
    sqlite3_stmt *stmt_get_pricing_ = nullptr;
    sqlite3_stmt *stmt_update_last_used_ = nullptr;
    sqlite3_stmt *stmt_insert_perf_event_ = nullptr;
    sqlite3_stmt *stmt_cleanup_perf_events_ = nullptr;
};
