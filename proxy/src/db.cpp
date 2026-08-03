#include "db.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <sys/file.h>
#include <unistd.h>
#include <utility>
#include <vector>
#include <sqlite3.h>

// ── Internal helpers ────────────────────────────────────────────────────

namespace {

// Lightweight RAII guard that calls sqlite3_finalize unless released.
class StmtGuard {
public:
    explicit StmtGuard(sqlite3_stmt *s) : stmt_(s) {}
    ~StmtGuard() { if (stmt_) sqlite3_finalize(stmt_); }
    sqlite3_stmt *release() { auto s = stmt_; stmt_ = nullptr; return s; }
private:
    sqlite3_stmt *stmt_;
};

} // anonymous namespace

// ── Constructor / Destructor ─────────────────────────────────────────────

Database::~Database() { close(); }

// ── open ─────────────────────────────────────────────────────────────────

bool Database::open(const std::string &path, const std::string &schema_dir) {
    int rc = sqlite3_open_v2(
        path.c_str(), &db_,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] Failed to open %s: %s\n", path.c_str(),
                sqlite3_errmsg(db_));
        return false;
    }
    db_path_ = path;

    // Performance / safety pragmas
    sqlite3_exec(db_, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
    sqlite3_exec(db_, "PRAGMA foreign_keys=ON", nullptr, nullptr, nullptr);
    sqlite3_exec(db_, "PRAGMA busy_timeout=5000", nullptr, nullptr, nullptr);

    if (!run_migrations(schema_dir)) {
        fprintf(stderr, "[DB] Schema migration failed — see errors above\n");
        sqlite3_close(db_);
        db_ = nullptr;
        return false;
    }

    // Clear any stale in-flight records from a previous run (e.g. crash)
    sqlite3_exec(db_, "DELETE FROM in_flight_requests", nullptr, nullptr, nullptr);

    prepare_statements();
    fprintf(stderr, "[DB] Opened %s (WAL mode)\n", path.c_str());
    return true;
}

void Database::close() {
    if (!db_) return;
    finalize_statements();
    sqlite3_close(db_);
    db_ = nullptr;
    fprintf(stderr, "[DB] Closed\n");
}

// ── Schema migrations ────────────────────────────────────────────────────
//
// Applies pending versioned migrations from `schema_dir` (schema/<db>/NNNN_*.sql),
// the single source of truth for the database schema (shared with the Python
// runner in app/migrations.py).  Each step runs inside
// "BEGIN IMMEDIATE; <body>; PRAGMA user_version = N; COMMIT;" under an advisory
// flock on <db>.migrate.lock, so concurrent runners (proxy + dashboard) and
// concurrent processes are serialized and every step is all-or-nothing.

bool Database::run_migrations(const std::string &schema_dir) {
    namespace fs = std::filesystem;
    if (!fs::is_directory(schema_dir)) {
        fprintf(stderr, "[DB] schema dir not found: %s\n", schema_dir.c_str());
        return false;
    }

    // Enumerate schema_dir/NNNN_*.sql and sort by the leading number.
    std::vector<std::pair<int, fs::path>> steps;
    for (const auto &e : fs::directory_iterator(schema_dir)) {
        std::string fn = e.path().filename().string();
        if (e.path().extension() != ".sql" || fn.size() < 5) continue;
        if (!isdigit(static_cast<unsigned char>(fn[0]))) continue;
        steps.emplace_back(std::stoi(fn.substr(0, 4)), e.path());
    }
    std::sort(steps.begin(), steps.end(),
              [](const auto &a, const auto &b) { return a.first < b.first; });
    if (steps.empty()) {
        fprintf(stderr, "[DB] no migration files in %s\n", schema_dir.c_str());
        return false;
    }

    // Advisory lock — pairs with the Python runner's fcntl.flock().
    int lock_fd = ::open((db_path_ + ".migrate.lock").c_str(), O_CREAT | O_RDWR, 0644);
    if (lock_fd < 0) {
        fprintf(stderr, "[DB] cannot open migration lock\n");
        return false;
    }
    flock(lock_fd, LOCK_EX);  // blocking

    // Current schema version.
    int version = 0;
    {
        sqlite3_stmt *s = nullptr;
        if (sqlite3_prepare_v2(db_, "PRAGMA user_version", -1, &s, nullptr) == SQLITE_OK
            && sqlite3_step(s) == SQLITE_ROW)
            version = sqlite3_column_int(s, 0);
        sqlite3_finalize(s);
    }

    bool ok = true;
    for (const auto &[n, p] : steps) {
        if (n <= version) continue;

        std::ifstream f(p);
        std::string body((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());
        if (body.empty()) {
            fprintf(stderr, "[DB] Migration %s is empty/unreadable\n", p.c_str());
            ok = false;
            break;
        }
        std::string sql = "BEGIN IMMEDIATE;\n" + body +
                          "\nPRAGMA user_version = " + std::to_string(n) + ";\nCOMMIT;";

        char *err = nullptr;
        if (sqlite3_exec(db_, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
            fprintf(stderr, "[DB] Migration %s failed: %s\n",
                    p.c_str(), err ? err : sqlite3_errmsg(db_));
            if (err) sqlite3_free(err);
            sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr);  // atomic step rollback
            ok = false;
            break;
        }
    }

    flock(lock_fd, LOCK_UN);
    ::close(lock_fd);
    return ok;
}


// ── Prepared statements ──────────────────────────────────────────────────

void Database::prepare_statements() {
    #define PREPARE(sql, stmt) \
        do { \
            int _rc = sqlite3_prepare_v2(db_, sql, -1, &stmt, nullptr); \
            if (_rc != SQLITE_OK) \
                fprintf(stderr, "[DB] Prepare error: %s\n", sqlite3_errmsg(db_)); \
        } while (0)

    PREPARE("SELECT id, key_value, account_id, "
            "COALESCE(label,'') "
            "FROM local_keys WHERE key_value = ?1",
            stmt_lookup_key_);

    PREPARE("SELECT id, name, upstream_key, base_url, api_format, "
            "COALESCE(endpoint_path,''), COALESCE(auth_header,'bearer'), "
            "COALESCE(is_aggregate,0), COALESCE(account_type,'api'), "
            "COALESCE(monthly_price,0), COALESCE(max_concurrency,0) "
            "FROM upstream_accounts WHERE id = ?1",
            stmt_get_account_);

    PREPARE("INSERT INTO request_log "
            "(account_id, local_key_id, model, prompt_tokens, "
            " completion_tokens, cache_read_tokens, total_tokens, cost, "
            " is_streaming, status_code, duration_ms) "
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
            stmt_insert_log_);

    PREPARE("SELECT pattern, upstream_account_id, upstream_model "
            "FROM aggregate_entries WHERE account_id = ?1 ORDER BY sort_order, id",
            stmt_get_aggregate_entries_);

    PREPARE("SELECT id, model_pattern, input_price, output_price "
            "FROM model_pricing ORDER BY id",
            stmt_get_pricing_);

    PREPARE("UPDATE local_keys SET last_used_at = datetime('now') "
            "WHERE id = ?1",
            stmt_update_last_used_);

    PREPARE("INSERT INTO perf_events "
            "(model, upstream_latency_ms, total_latency_ms, status_code, is_error, concurrent_count) "
            "VALUES (?1,?2,?3,?4,?5,?6)",
            stmt_insert_perf_event_);

    PREPARE("DELETE FROM perf_events "
            "WHERE requested_at < datetime('now', '-' || ?1 || ' minutes')",
            stmt_cleanup_perf_events_);

    PREPARE("INSERT INTO in_flight_requests "
            "(local_key_id, account_id, model, is_streaming) "
            "VALUES (?1,?2,?3,?4)",
            stmt_insert_in_flight_);

    PREPARE("DELETE FROM in_flight_requests WHERE id = ?1",
            stmt_delete_in_flight_);

    PREPARE("SELECT COUNT(*) FROM in_flight_requests",
            stmt_count_in_flight_);

    PREPARE("DELETE FROM in_flight_requests "
            "WHERE started_at < datetime('now', '-' || ?1 || ' minutes')",
            stmt_cleanup_in_flight_);

    #undef PREPARE
}

void Database::finalize_statements() {
    #define FINALIZE(s) do { if (s) { sqlite3_finalize(s); s = nullptr; } } while (0)
    FINALIZE(stmt_lookup_key_);
    FINALIZE(stmt_get_account_);
    FINALIZE(stmt_insert_log_);
    FINALIZE(stmt_get_aggregate_entries_);
    FINALIZE(stmt_get_pricing_);
    FINALIZE(stmt_update_last_used_);
    FINALIZE(stmt_insert_perf_event_);
    FINALIZE(stmt_cleanup_perf_events_);
    FINALIZE(stmt_insert_in_flight_);
    FINALIZE(stmt_delete_in_flight_);
    FINALIZE(stmt_count_in_flight_);
    FINALIZE(stmt_cleanup_in_flight_);
    #undef FINALIZE
}

// ── lookup_local_key ─────────────────────────────────────────────────────

std::optional<Database::KeyInfo> Database::lookup_local_key(
    const std::string &key_value) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_lookup_key_);
    sqlite3_bind_text(stmt_lookup_key_, 1,
                      key_value.c_str(), key_value.size(), SQLITE_STATIC);

    std::optional<KeyInfo> result;
    if (sqlite3_step(stmt_lookup_key_) == SQLITE_ROW) {
        KeyInfo info;
        info.id = sqlite3_column_int(stmt_lookup_key_, 0);
        info.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_key_, 1));
        info.account_id = sqlite3_column_int(stmt_lookup_key_, 2);
        info.label = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_key_, 3));
        result = std::move(info);
    }
    sqlite3_reset(stmt_lookup_key_);
    return result;
}

// ── get_account ──────────────────────────────────────────────────────────

std::optional<Database::AccountInfo> Database::get_account(int account_id) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_get_account_);
    sqlite3_bind_int(stmt_get_account_, 1, account_id);

    std::optional<AccountInfo> result;
    if (sqlite3_step(stmt_get_account_) == SQLITE_ROW) {
        AccountInfo info;
        info.id = sqlite3_column_int(stmt_get_account_, 0);
        info.name = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 1));
        info.upstream_key = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 2));
        info.base_url = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 3));
        info.api_format = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 4));
        if (info.api_format.empty()) info.api_format = "openai";
        info.endpoint_path = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 5));
        info.auth_header = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 6));
        if (info.auth_header.empty()) info.auth_header = "bearer";
        info.is_aggregate = sqlite3_column_int(stmt_get_account_, 7) != 0;
        const char *atype = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 8));
        info.account_type = atype ? atype : "api";
        if (info.account_type.empty()) info.account_type = "api";
        info.monthly_price = sqlite3_column_double(stmt_get_account_, 9);
        info.max_concurrency = sqlite3_column_int(stmt_get_account_, 10);
        result = std::move(info);
    }
    sqlite3_reset(stmt_get_account_);
    return result;
}

// ── log_request ──────────────────────────────────────────────────────────

void Database::log_request(int account_id, int local_key_id,
                           const std::string &model,
                           int prompt_tokens, int completion_tokens,
                           int cache_read_tokens, int total_tokens,
                           double cost,
                           bool is_streaming, int status_code,
                           int duration_ms) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_insert_log_);
    sqlite3_bind_int(stmt_insert_log_, 1, account_id);
    sqlite3_bind_int(stmt_insert_log_, 2, local_key_id);
    sqlite3_bind_text(stmt_insert_log_, 3,
                      model.c_str(), model.size(), SQLITE_STATIC);
    sqlite3_bind_int(stmt_insert_log_, 4, prompt_tokens);
    sqlite3_bind_int(stmt_insert_log_, 5, completion_tokens);
    sqlite3_bind_int(stmt_insert_log_, 6, cache_read_tokens);
    sqlite3_bind_int(stmt_insert_log_, 7, total_tokens);
    sqlite3_bind_double(stmt_insert_log_, 8, cost);
    sqlite3_bind_int(stmt_insert_log_, 9, is_streaming ? 1 : 0);
    sqlite3_bind_int(stmt_insert_log_, 10, status_code);
    sqlite3_bind_int(stmt_insert_log_, 11, duration_ms);

    int rc = sqlite3_step(stmt_insert_log_);
    if (rc != SQLITE_DONE)
        fprintf(stderr, "[DB] log_request insert error: %s\n",
                sqlite3_errmsg(db_));
    sqlite3_reset(stmt_insert_log_);
}

// ── update_key_last_used ─────────────────────────────────────────────────

void Database::update_key_last_used(int local_key_id) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_update_last_used_);
    sqlite3_bind_int(stmt_update_last_used_, 1, local_key_id);
    sqlite3_step(stmt_update_last_used_);
    sqlite3_reset(stmt_update_last_used_);
}

// ── resolve_aggregate ────────────────────────────────────────────────────

std::vector<Database::AggregateEntry>
Database::resolve_aggregate(int account_id, const std::string &model) {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<AggregateEntry> result;
    sqlite3_reset(stmt_get_aggregate_entries_);
    sqlite3_bind_int(stmt_get_aggregate_entries_, 1, account_id);
    while (sqlite3_step(stmt_get_aggregate_entries_) == SQLITE_ROW) {
        std::string pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_aggregate_entries_, 0));
        // Exact (case-sensitive) match: entries are concrete model names —
        // the aggregate's model catalog, not wildcard patterns.  One model
        // may map to several upstream accounts (priority = sort_order, id,
        // already applied by the query); collect ALL matches so the caller
        // can try them in order, skipping busy / cooling-down accounts.
        if (pattern == model) {
            AggregateEntry e;
            e.pattern = pattern;
            e.upstream_account_id = sqlite3_column_int(stmt_get_aggregate_entries_, 1);
            e.upstream_model = reinterpret_cast<const char *>(
                sqlite3_column_text(stmt_get_aggregate_entries_, 2));
            result.push_back(std::move(e));
        }
    }
    sqlite3_reset(stmt_get_aggregate_entries_);
    return result;
}

// ── get_aggregate_model_patterns ─────────────────────────────────────────

std::vector<std::string>
Database::get_aggregate_model_patterns(int account_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<std::string> result;
    sqlite3_reset(stmt_get_aggregate_entries_);
    sqlite3_bind_int(stmt_get_aggregate_entries_, 1, account_id);
    while (sqlite3_step(stmt_get_aggregate_entries_) == SQLITE_ROW) {
        result.emplace_back(reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_aggregate_entries_, 0)));
    }
    sqlite3_reset(stmt_get_aggregate_entries_);
    return result;
}

// ── get_all_pricing ─────────────────────────────────────────────────────

std::vector<Database::PricingEntry> Database::get_all_pricing() {
    std::lock_guard<std::mutex> lock(mutex_);

    std::vector<PricingEntry> result;
    sqlite3_reset(stmt_get_pricing_);

    while (sqlite3_step(stmt_get_pricing_) == SQLITE_ROW) {
        PricingEntry e;
        e.id = sqlite3_column_int(stmt_get_pricing_, 0);
        e.model_pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_pricing_, 1));
        e.input_price = sqlite3_column_double(stmt_get_pricing_, 2);
        e.output_price = sqlite3_column_double(stmt_get_pricing_, 3);
        result.push_back(e);
    }
    sqlite3_reset(stmt_get_pricing_);
    return result;
}

// ── log_perf_event ───────────────────────────────────────────────────────

void Database::log_perf_event(const std::string &model,
                              int upstream_latency_ms, int total_latency_ms,
                              int status_code, bool is_error,
                              int concurrent_count) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_insert_perf_event_);
    sqlite3_bind_text(stmt_insert_perf_event_, 1,
                      model.c_str(), model.size(), SQLITE_STATIC);
    sqlite3_bind_int(stmt_insert_perf_event_, 2, upstream_latency_ms);
    sqlite3_bind_int(stmt_insert_perf_event_, 3, total_latency_ms);
    sqlite3_bind_int(stmt_insert_perf_event_, 4, status_code);
    sqlite3_bind_int(stmt_insert_perf_event_, 5, is_error ? 1 : 0);
    sqlite3_bind_int(stmt_insert_perf_event_, 6, concurrent_count);

    int rc = sqlite3_step(stmt_insert_perf_event_);
    if (rc != SQLITE_DONE)
        fprintf(stderr, "[DB] log_perf_event insert error: %s\n",
                sqlite3_errmsg(db_));
    sqlite3_reset(stmt_insert_perf_event_);
}

// ── cleanup_old_perf_events ──────────────────────────────────────────────

void Database::cleanup_old_perf_events(int max_age_minutes) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_cleanup_perf_events_);
    sqlite3_bind_int(stmt_cleanup_perf_events_, 1, max_age_minutes);
    sqlite3_step(stmt_cleanup_perf_events_);
    sqlite3_reset(stmt_cleanup_perf_events_);
}

// ── in_flight_requests tracking ─────────────────────────────────────────

int Database::request_start(int local_key_id, int account_id,
                             const std::string &model, bool is_streaming) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_insert_in_flight_);
    sqlite3_bind_int(stmt_insert_in_flight_, 1, local_key_id);
    sqlite3_bind_int(stmt_insert_in_flight_, 2, account_id);
    sqlite3_bind_text(stmt_insert_in_flight_, 3,
                      model.c_str(), model.size(), SQLITE_STATIC);
    sqlite3_bind_int(stmt_insert_in_flight_, 4, is_streaming ? 1 : 0);

    int rc = sqlite3_step(stmt_insert_in_flight_);
    if (rc != SQLITE_DONE)
        fprintf(stderr, "[DB] request_start insert error: %s\n",
                sqlite3_errmsg(db_));
    sqlite3_reset(stmt_insert_in_flight_);
    return static_cast<int>(sqlite3_last_insert_rowid(db_));
}

void Database::request_end(int row_id) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_delete_in_flight_);
    sqlite3_bind_int(stmt_delete_in_flight_, 1, row_id);
    int rc = sqlite3_step(stmt_delete_in_flight_);
    if (rc != SQLITE_DONE)
        fprintf(stderr, "[DB] request_end delete error: %s\n",
                sqlite3_errmsg(db_));
    sqlite3_reset(stmt_delete_in_flight_);
}

int Database::get_in_flight_count() {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_count_in_flight_);
    int count = 0;
    if (sqlite3_step(stmt_count_in_flight_) == SQLITE_ROW)
        count = sqlite3_column_int(stmt_count_in_flight_, 0);
    sqlite3_reset(stmt_count_in_flight_);
    return count;
}

void Database::cleanup_stale_in_flight(int max_age_minutes) {
    std::lock_guard<std::mutex> lock(mutex_);

    sqlite3_reset(stmt_cleanup_in_flight_);
    sqlite3_bind_int(stmt_cleanup_in_flight_, 1, max_age_minutes);
    sqlite3_step(stmt_cleanup_in_flight_);
    sqlite3_reset(stmt_cleanup_in_flight_);
}
