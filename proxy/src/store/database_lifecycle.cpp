#include "database_internal.h"

bool Database::open(const std::string &path, const std::string &schema_dir) {
    std::unique_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    if (write_db_ || read_db_ || pricing_db_) {
        TB_LOG_ERROR( "[DB] open called on an already-open database\n");
        return false;
    }
    int rc = sqlite3_open_v2(
        path.c_str(), &write_db_,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    if (rc != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] Failed to open %s: %s\n", path.c_str(),
                sqlite3_errmsg(write_db_));
        return false;
    }
    db_path_ = path;

    // Performance / safety pragmas
    sqlite3_exec(write_db_, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
    sqlite3_exec(write_db_, "PRAGMA foreign_keys=ON", nullptr, nullptr, nullptr);
    sqlite3_exec(write_db_, "PRAGMA synchronous=FULL", nullptr, nullptr, nullptr);
    sqlite3_exec(write_db_, "PRAGMA busy_timeout=5000", nullptr, nullptr, nullptr);

    if (!run_migrations(schema_dir)) {
        TB_LOG_ERROR( "[DB] Schema migration failed — see errors above\n");
        sqlite3_close(write_db_);
        write_db_ = nullptr;
        return false;
    }
    {
        sqlite3_stmt *version_stmt = nullptr;
        if (sqlite3_prepare_v2(write_db_, "PRAGMA user_version", -1,
                               &version_stmt, nullptr) == SQLITE_OK &&
            sqlite3_step(version_stmt) == SQLITE_ROW)
        {
            const int encoded = sqlite3_column_int(version_stmt, 0);
            schema_major_ = encoded / 10000;
            schema_minor_ = encoded % 10000;
        }
        sqlite3_finalize(version_stmt);
    }
    if (schema_major_ != 1) {
        TB_LOG_ERROR(
                "[DB] runtime accepts only V1 databases; received V%d. "
                "Run the offline/local upgrade coordinator first\n",
                schema_major_);
        sqlite3_close(write_db_);
        write_db_ = nullptr;
        return false;
    }

    rc = sqlite3_open_v2(path.c_str(), &read_db_,
                         SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (rc != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] Failed to open read connection for %s: %s\n",
                path.c_str(), sqlite3_errmsg(read_db_));
        if (read_db_) sqlite3_close(read_db_);
        read_db_ = nullptr;
        sqlite3_close(write_db_);
        write_db_ = nullptr;
        return false;
    }
    sqlite3_exec(read_db_, "PRAGMA foreign_keys=ON", nullptr, nullptr, nullptr);
    sqlite3_exec(read_db_, "PRAGMA busy_timeout=5000", nullptr, nullptr, nullptr);

    // Pricing snapshots are isolated from route/auth lookups.  This connection
    // never writes and deliberately uses a very short busy timeout: a logging
    // tail must not convoy the routing read connection behind schema activity.
    rc = sqlite3_open_v2(path.c_str(), &pricing_db_,
                         SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (rc != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] Failed to open pricing snapshot connection: %s\n",
                pricing_db_ ? sqlite3_errmsg(pricing_db_) : "unknown error");
        if (pricing_db_) sqlite3_close(pricing_db_);
        pricing_db_ = nullptr;
        sqlite3_close(read_db_);
        sqlite3_close(write_db_);
        read_db_ = nullptr;
        write_db_ = nullptr;
        return false;
    }
    sqlite3_exec(pricing_db_, "PRAGMA query_only=ON", nullptr, nullptr, nullptr);
    sqlite3_exec(pricing_db_, "PRAGMA busy_timeout=0", nullptr, nullptr, nullptr);

    if (!prepare_statements()) {
        TB_LOG_ERROR( "[DB] Failed to prepare required statements\n");
        finalize_statements();
        sqlite3_close(pricing_db_);
        sqlite3_close(read_db_);
        sqlite3_close(write_db_);
        pricing_db_ = nullptr;
        read_db_ = nullptr;
        write_db_ = nullptr;
        return false;
    }
    if (!start_log_writer()) {
        TB_LOG_ERROR( "[DB] Failed to start request-log writer\n");
        finalize_statements();
        sqlite3_close(pricing_db_);
        sqlite3_close(read_db_);
        sqlite3_close(write_db_);
        pricing_db_ = nullptr;
        read_db_ = nullptr;
        write_db_ = nullptr;
        return false;
    }
    TB_LOG_INFO("[DB] Opened %s (WAL mode)\n", path.c_str());
    return true;
}

void Database::close() {
    std::unique_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    // The writer owns prepared log statements while it drains.  It must be
    // joined before either statements or connections are released.
    stop_log_writer();
    if (!write_db_ && !read_db_ && !pricing_db_) return;
    std::scoped_lock db_locks(write_mutex_, read_mutex_, pricing_mutex_);
    finalize_statements();
    if (pricing_db_) sqlite3_close(pricing_db_);
    if (read_db_) sqlite3_close(read_db_);
    if (write_db_) sqlite3_close(write_db_);
    pricing_db_ = nullptr;
    read_db_ = nullptr;
    write_db_ = nullptr;
    const auto failures = log_persist_failures_.load(std::memory_order_relaxed);
    if (failures != 0) {
        TB_LOG_ERROR(
                "[DB] Closed after %llu request-log persistence failure(s); "
                "see preceding errors\n",
                static_cast<unsigned long long>(failures));
    } else {
        TB_LOG_INFO("[DB] Closed\n");
    }
}

// ── Schema migrations ────────────────────────────────────────────────────
//
// The protocol is shared with app/db/migrations.py.  Files are numerically
// ordered major-minor pairs below schema/proxy/vN.  Same-major minor upgrades
// are automatic; a major transition is always an explicit maintenance action.
