#include "db.h"

#include <cstdio>
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

bool Database::open(const std::string &path) {
    int rc = sqlite3_open_v2(
        path.c_str(), &db_,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] Failed to open %s: %s\n", path.c_str(),
                sqlite3_errmsg(db_));
        return false;
    }

    // Performance / safety pragmas
    sqlite3_exec(db_, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
    sqlite3_exec(db_, "PRAGMA foreign_keys=ON", nullptr, nullptr, nullptr);
    sqlite3_exec(db_, "PRAGMA busy_timeout=5000", nullptr, nullptr, nullptr);

    create_schema();

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

// ── Schema creation ──────────────────────────────────────────────────────

void Database::create_schema() {
    const char *sql = R"SQL(
        CREATE TABLE IF NOT EXISTS upstream_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            upstream_key TEXT NOT NULL,
            base_url    TEXT NOT NULL DEFAULT '',
            api_format  TEXT NOT NULL DEFAULT 'openai',
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS local_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value   TEXT NOT NULL UNIQUE,
            label       TEXT,
            account_id  INTEGER NOT NULL REFERENCES upstream_accounts(id),
            template_id INTEGER REFERENCES model_map_templates(id),
            created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            last_used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS request_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id       INTEGER NOT NULL REFERENCES upstream_accounts(id),
            local_key_id     INTEGER NOT NULL REFERENCES local_keys(id),
            model            TEXT NOT NULL,
            prompt_tokens    INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens     INTEGER NOT NULL DEFAULT 0,
            cost             REAL NOT NULL DEFAULT 0.0,
            exported         INTEGER NOT NULL DEFAULT 0,
            is_streaming     INTEGER NOT NULL DEFAULT 0,
            status_code      INTEGER NOT NULL,
            duration_ms      INTEGER NOT NULL DEFAULT 0,
            requested_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_rl_account
            ON request_log(account_id);
        CREATE INDEX IF NOT EXISTS idx_rl_time
            ON request_log(requested_at);
        CREATE INDEX IF NOT EXISTS idx_rl_exported
            ON request_log(exported);

        CREATE TABLE IF NOT EXISTS model_pricing (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            model_pattern  TEXT NOT NULL UNIQUE,
            input_price    REAL NOT NULL,
            output_price   REAL NOT NULL,
            currency       TEXT NOT NULL DEFAULT 'CNY'
        );

        CREATE TABLE IF NOT EXISTS account_models (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL REFERENCES upstream_accounts(id),
            model_id    TEXT NOT NULL,
            UNIQUE(account_id, model_id)
        );

        CREATE TABLE IF NOT EXISTS key_model_map (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id          INTEGER NOT NULL REFERENCES local_keys(id),
            pattern         TEXT NOT NULL,
            upstream_model  TEXT NOT NULL,
            UNIQUE(key_id, pattern)
        );

        CREATE TABLE IF NOT EXISTS model_map_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            sort_order   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS model_map_template_entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id     INTEGER NOT NULL REFERENCES model_map_templates(id) ON DELETE CASCADE,
            sort_order       INTEGER NOT NULL DEFAULT 0,
            pattern         TEXT NOT NULL,
            upstream_model  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS perf_events (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            model               TEXT NOT NULL,
            upstream_latency_ms INTEGER NOT NULL DEFAULT 0,
            total_latency_ms    INTEGER NOT NULL DEFAULT 0,
            status_code         INTEGER NOT NULL,
            is_error            INTEGER NOT NULL DEFAULT 0,
            concurrent_count    INTEGER NOT NULL DEFAULT 0,
            requested_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_perf_events_time
            ON perf_events(requested_at);

        CREATE TABLE IF NOT EXISTS in_flight_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            local_key_id    INTEGER NOT NULL,
            account_id      INTEGER NOT NULL,
            model           TEXT NOT NULL,
            is_streaming    INTEGER NOT NULL DEFAULT 0,
            started_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Trigger: auto-compute cost from model_pricing when a request is logged.
        -- Removes the need for application-level cost computation.
        CREATE TRIGGER IF NOT EXISTS tr_request_log_insert
        AFTER INSERT ON request_log
        BEGIN
            UPDATE request_log SET cost = COALESCE((
                SELECT (NEW.prompt_tokens / 1000000.0) * mp.input_price
                     + (NEW.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0) WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
        END;

        -- Trigger: recalculate all costs when a pricing entry is inserted.
        CREATE TRIGGER IF NOT EXISTS tr_pricing_insert
        AFTER INSERT ON model_pricing
        BEGIN
            UPDATE request_log SET cost = COALESCE((
                SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0);
        END;

        -- Trigger: recalculate all costs when a pricing entry is updated.
        CREATE TRIGGER IF NOT EXISTS tr_pricing_update
        AFTER UPDATE ON model_pricing
        BEGIN
            UPDATE request_log SET cost = COALESCE((
                SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0);
        END;

        -- Trigger: recalculate all costs when a pricing entry is deleted.
        CREATE TRIGGER IF NOT EXISTS tr_pricing_delete
        AFTER DELETE ON model_pricing
        BEGIN
            UPDATE request_log SET cost = COALESCE((
                SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                     + (request_log.completion_tokens / 1000000.0) * mp.output_price
                FROM model_pricing mp
                WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                ORDER BY mp.id LIMIT 1
            ), 0.0);
        END;
    )SQL";

    char *err = nullptr;
    int rc = sqlite3_exec(db_, sql, nullptr, nullptr, &err);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] Schema creation error: %s\n", err);
        sqlite3_free(err);
    }

    // ── Migrations ─────────────────────────────────────────────────────
    // v2: add api_format column to upstream_accounts (added 2026-06)
    {
        // Check if the column already exists
        bool has_api_format = false;
        sqlite3_stmt *stmt = nullptr;
        sqlite3_prepare_v2(db_,
            "SELECT api_format FROM upstream_accounts LIMIT 1",
            -1, &stmt, nullptr);
        if (stmt) {
            has_api_format = (sqlite3_step(stmt) == SQLITE_ROW);
            sqlite3_finalize(stmt);
        }
        if (!has_api_format) {
            sqlite3_exec(db_,
                "ALTER TABLE upstream_accounts ADD COLUMN api_format "
                "TEXT NOT NULL DEFAULT 'openai'",
                nullptr, nullptr, &err);
            if (err) {
                fprintf(stderr, "[DB] Migration v2 error: %s\n", err);
                sqlite3_free(err);
                err = nullptr;
            } else {
                fprintf(stderr, "[DB] Migration v2: added api_format column\n");
            }
        }
    }

    // Pricing entries are managed by the web dashboard; no auto-seeding.
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
            "COALESCE(label,'') FROM local_keys "
            "WHERE key_value = ?1",
            stmt_lookup_key_);

    PREPARE("SELECT id, name, upstream_key, base_url, api_format "
            "FROM upstream_accounts WHERE id = ?1",
            stmt_get_account_);

    PREPARE("INSERT INTO request_log "
            "(account_id, local_key_id, model, prompt_tokens, "
            " completion_tokens, total_tokens, cost, is_streaming, "
            " status_code, duration_ms) "
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            stmt_insert_log_);

    PREPARE("SELECT pattern, upstream_model FROM key_model_map "
            "WHERE key_id = ?1 ORDER BY id",
            stmt_get_key_mappings_);

    PREPARE("SELECT template_id FROM local_keys WHERE id = ?1",
            stmt_get_key_template_);

    PREPARE("SELECT pattern, upstream_model FROM model_map_template_entries "
            "WHERE template_id = ?1 ORDER BY sort_order, id",
            stmt_get_template_entries_);

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
    FINALIZE(stmt_get_key_mappings_);
    FINALIZE(stmt_get_key_template_);
    FINALIZE(stmt_get_template_entries_);
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
        result = std::move(info);
    }
    sqlite3_reset(stmt_get_account_);
    return result;
}

// ── log_request ──────────────────────────────────────────────────────────

void Database::log_request(int account_id, int local_key_id,
                           const std::string &model,
                           int prompt_tokens, int completion_tokens,
                           int total_tokens, double cost,
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
    sqlite3_bind_int(stmt_insert_log_, 6, total_tokens);
    sqlite3_bind_double(stmt_insert_log_, 7, cost);
    sqlite3_bind_int(stmt_insert_log_, 8, is_streaming ? 1 : 0);
    sqlite3_bind_int(stmt_insert_log_, 9, status_code);
    sqlite3_bind_int(stmt_insert_log_, 10, duration_ms);

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

// ── get_key_template_id ──────────────────────────────────────────────────

int Database::get_key_template_id(int key_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    sqlite3_reset(stmt_get_key_template_);
    sqlite3_bind_int(stmt_get_key_template_, 1, key_id);
    int tid = 0;
    if (sqlite3_step(stmt_get_key_template_) == SQLITE_ROW)
        tid = sqlite3_column_type(stmt_get_key_template_, 0) != SQLITE_NULL
                  ? sqlite3_column_int(stmt_get_key_template_, 0) : 0;
    sqlite3_reset(stmt_get_key_template_);
    return tid;
}

// ── get_template_entries ─────────────────────────────────────────────────

std::vector<Database::ModelMapping> Database::get_template_entries(int template_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<ModelMapping> result;
    sqlite3_reset(stmt_get_template_entries_);
    sqlite3_bind_int(stmt_get_template_entries_, 1, template_id);
    while (sqlite3_step(stmt_get_template_entries_) == SQLITE_ROW) {
        ModelMapping m;
        m.pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_template_entries_, 0));
        m.upstream_model = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_template_entries_, 1));
        result.push_back(m);
    }
    sqlite3_reset(stmt_get_template_entries_);
    return result;
}

// ── get_key_model_mappings ──────────────────────────────────────────────

std::vector<Database::ModelMapping> Database::get_key_model_mappings(int key_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<ModelMapping> result;
    sqlite3_reset(stmt_get_key_mappings_);
    sqlite3_bind_int(stmt_get_key_mappings_, 1, key_id);

    while (sqlite3_step(stmt_get_key_mappings_) == SQLITE_ROW) {
        ModelMapping m;
        m.pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_key_mappings_, 0));
        m.upstream_model = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_key_mappings_, 1));
        result.push_back(m);
    }
    sqlite3_reset(stmt_get_key_mappings_);
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
