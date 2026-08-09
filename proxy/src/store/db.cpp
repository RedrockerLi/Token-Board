#include "db.h"
#include "json.hpp"
#include "core/account_types.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <exception>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <limits>
#include <iomanip>
#include <regex>
#include <sstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <tuple>
#include <unistd.h>
#include <utility>
#include <vector>
#include <openssl/sha.h>
#include <sqlite3.h>

// ── Internal helpers ────────────────────────────────────────────────────

namespace {

using json = nlohmann::json;

constexpr std::size_t kSpoolHeaderBytes = 8;

std::uint32_t spool_checksum(const char *data, std::size_t size) {
    std::uint32_t hash = 2166136261u;
    for (std::size_t i = 0; i < size; ++i) {
        hash ^= static_cast<unsigned char>(data[i]);
        hash *= 16777619u;
    }
    return hash;
}

void append_u32_le(std::string &out, std::uint32_t value) {
    for (int shift = 0; shift < 32; shift += 8)
        out.push_back(static_cast<char>((value >> shift) & 0xffu));
}

std::uint32_t read_u32_le(const char *data) {
    std::uint32_t value = 0;
    for (int i = 0; i < 4; ++i)
        value |= static_cast<std::uint32_t>(
                     static_cast<unsigned char>(data[i])) << (i * 8);
    return value;
}

bool pread_exact(int fd, void *buffer, std::size_t size, std::uint64_t offset) {
    auto *out = static_cast<char *>(buffer);
    std::size_t done = 0;
    while (done < size) {
        const ssize_t n = ::pread(fd, out + done, size - done,
                                  static_cast<off_t>(offset + done));
        if (n == 0) return false;
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        done += static_cast<std::size_t>(n);
    }
    return true;
}

bool write_exact(int fd, const char *data, std::size_t size) {
    std::size_t done = 0;
    while (done < size) {
        const ssize_t n = ::write(fd, data + done, size - done);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        done += static_cast<std::size_t>(n);
    }
    return true;
}

std::string bounded_string(const std::string &value, std::size_t limit) {
    if (value.size() <= limit) return value;
    return value.substr(0, limit);
}

// Lightweight RAII guard that calls sqlite3_finalize unless released.
class StmtGuard {
public:
    explicit StmtGuard(sqlite3_stmt *s) : stmt_(s) {}
    ~StmtGuard() { if (stmt_) sqlite3_finalize(stmt_); }
    sqlite3_stmt *release() { auto s = stmt_; stmt_ = nullptr; return s; }
private:
    sqlite3_stmt *stmt_;
};

class ReadTransactionGuard {
public:
    explicit ReadTransactionGuard(sqlite3 *db) : db_(db) {}
    ~ReadTransactionGuard() {
        if (db_) sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr);
    }
    void release() noexcept { db_ = nullptr; }
private:
    sqlite3 *db_;
};

} // anonymous namespace

// ── Constructor / Destructor ─────────────────────────────────────────────

Database::~Database() { close(); }

// ── open ─────────────────────────────────────────────────────────────────

bool Database::open(const std::string &path, const std::string &schema_dir) {
    std::unique_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    if (write_db_ || read_db_ || pricing_db_) {
        fprintf(stderr, "[DB] open called on an already-open database\n");
        return false;
    }
    int rc = sqlite3_open_v2(
        path.c_str(), &write_db_,
        SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
        nullptr);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] Failed to open %s: %s\n", path.c_str(),
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
        fprintf(stderr, "[DB] Schema migration failed — see errors above\n");
        sqlite3_close(write_db_);
        write_db_ = nullptr;
        return false;
    }
    {
        sqlite3_stmt *version_stmt = nullptr;
        if (sqlite3_prepare_v2(write_db_, "PRAGMA user_version", -1,
                               &version_stmt, nullptr) == SQLITE_OK &&
            sqlite3_step(version_stmt) == SQLITE_ROW)
            schema_major_ = sqlite3_column_int(version_stmt, 0) / 10000;
        sqlite3_finalize(version_stmt);
    }

    rc = sqlite3_open_v2(path.c_str(), &read_db_,
                         SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] Failed to open read connection for %s: %s\n",
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
        fprintf(stderr, "[DB] Failed to open pricing snapshot connection: %s\n",
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
        fprintf(stderr, "[DB] Failed to prepare required statements\n");
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
        fprintf(stderr, "[DB] Failed to start request-log writer\n");
        finalize_statements();
        sqlite3_close(pricing_db_);
        sqlite3_close(read_db_);
        sqlite3_close(write_db_);
        pricing_db_ = nullptr;
        read_db_ = nullptr;
        write_db_ = nullptr;
        return false;
    }
    fprintf(stderr, "[DB] Opened %s (WAL mode)\n", path.c_str());
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
        fprintf(stderr,
                "[DB] Closed after %llu request-log persistence failure(s); "
                "see preceding errors\n",
                static_cast<unsigned long long>(failures));
    } else {
        fprintf(stderr, "[DB] Closed\n");
    }
}

// ── Schema migrations ────────────────────────────────────────────────────
//
// The protocol is shared with app/db/migrations.py.  Files are numerically
// ordered major-minor pairs below schema/proxy/vN.  Same-major minor upgrades
// are automatic; a major transition is always an explicit maintenance action.

bool Database::run_migrations(const std::string &schema_dir) {
    namespace fs = std::filesystem;
    // Advisory lock — pairs with the Python runner's fcntl.flock().
    int lock_fd = ::open((db_path_ + ".migrate.lock").c_str(), O_CREAT | O_RDWR, 0644);
    if (lock_fd < 0) {
        fprintf(stderr, "[DB] cannot open migration lock\n");
        return false;
    }
    if (flock(lock_fd, LOCK_EX) != 0) {  // blocking
        fprintf(stderr, "[DB] cannot lock migration file: %s\n",
                std::strerror(errno));
        ::close(lock_fd);
        return false;
    }

    int encoded_version = 0;
    {
        sqlite3_stmt *s = nullptr;
        if (sqlite3_prepare_v2(write_db_, "PRAGMA user_version", -1, &s, nullptr) == SQLITE_OK
            && sqlite3_step(s) == SQLITE_ROW)
            encoded_version = sqlite3_column_int(s, 0);
        sqlite3_finalize(s);
    }
    {
        sqlite3_stmt *s = nullptr;
        const char *sql =
            "SELECT major,minor,database_name FROM schema_version WHERE id=1";
        if (sqlite3_prepare_v2(write_db_, sql, -1, &s, nullptr) == SQLITE_OK &&
            sqlite3_step(s) == SQLITE_ROW) {
            const int major = sqlite3_column_int(s, 0);
            const int minor = sqlite3_column_int(s, 1);
            const unsigned char *name = sqlite3_column_text(s, 2);
            if (encoded_version != major * 10000 + minor || !name ||
                std::string(reinterpret_cast<const char *>(name)) != "proxy") {
                fprintf(stderr, "[DB] schema_version disagrees with "
                        "PRAGMA user_version or database identity\n");
                sqlite3_finalize(s);
                flock(lock_fd, LOCK_UN);
                ::close(lock_fd);
                return false;
            }
        }
        sqlite3_finalize(s);
    }

    bool empty_database = encoded_version == 0;
    if (empty_database) {
        sqlite3_stmt *s = nullptr;
        const char *sql =
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
            "('schema_version','schema_migrations')";
        if (sqlite3_prepare_v2(write_db_, sql, -1, &s, nullptr) == SQLITE_OK &&
            sqlite3_step(s) == SQLITE_ROW)
            empty_database = sqlite3_column_int(s, 0) == 0;
        sqlite3_finalize(s);
        if (!empty_database) {
            fprintf(stderr, "[DB] non-empty database has no schema version\n");
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
    }
    const int current_major = encoded_version / 10000;
    const int current_minor = encoded_version % 10000;

    fs::path supplied(schema_dir);
    if (!fs::is_directory(supplied)) {
        fprintf(stderr, "[DB] schema dir not found: %s\n", schema_dir.c_str());
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return false;
    }
    const std::regex filename_re(R"(^(\d+)-(\d+)_([a-z0-9][a-z0-9_]*)\.sql$)");
    auto contains_steps = [&](const fs::path &path) {
        for (const auto &e : fs::directory_iterator(path))
            if (e.path().extension() == ".sql") return true;
        return false;
    };

    fs::path selected = supplied;
    if (!contains_steps(selected)) {
        fs::path database_root = selected.filename() == "proxy"
            ? selected : selected / "proxy";
        std::vector<std::pair<int, fs::path>> majors;
        if (fs::is_directory(database_root)) {
            for (const auto &e : fs::directory_iterator(database_root)) {
                const auto name = e.path().filename().string();
                if (!e.is_directory() || name.size() < 2 || name[0] != 'v' ||
                    !std::all_of(name.begin() + 1, name.end(), [](unsigned char c) {
                        return std::isdigit(c) != 0;
                    })) continue;
                majors.emplace_back(std::stoi(name.substr(1)), e.path());
            }
        }
        std::sort(majors.begin(), majors.end());
        if (majors.empty()) {
            fprintf(stderr, "[DB] no schema/proxy/vN directory below %s\n",
                    schema_dir.c_str());
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
        selected = majors.back().second;
        if (!empty_database && current_major != majors.back().first) {
                fprintf(stderr,
                        "[DB] proxy schema is V%d.%d but program uses V%d; "
                        "run schema/transitions/%d-to-%d\n",
                        current_major, current_minor, majors.back().first,
                        current_major, majors.back().first);
                flock(lock_fd, LOCK_UN);
                ::close(lock_fd);
                return false;
        }
    }

    struct Step { int major; int minor; fs::path path; std::string checksum; };
    std::vector<Step> steps;
    for (const auto &e : fs::directory_iterator(selected)) {
        if (e.path().extension() != ".sql") continue;
        const std::string fn = e.path().filename().string();
        std::smatch match;
        if (!std::regex_match(fn, match, filename_re)) {
            fprintf(stderr, "[DB] bad migration filename: %s "
                    "(need major-minor_description.sql)\n", fn.c_str());
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
        std::ifstream input(e.path(), std::ios::binary);
        std::string body((std::istreambuf_iterator<char>(input)),
                         std::istreambuf_iterator<char>());
        unsigned char digest[SHA256_DIGEST_LENGTH];
        SHA256(reinterpret_cast<const unsigned char *>(body.data()), body.size(), digest);
        std::ostringstream hex;
        for (unsigned char byte : digest)
            hex << std::hex << std::setw(2) << std::setfill('0')
                << static_cast<int>(byte);
        steps.push_back({std::stoi(match[1]), std::stoi(match[2]), e.path(), hex.str()});
    }
    std::sort(steps.begin(), steps.end(), [](const Step &a, const Step &b) {
        return std::tie(a.major, a.minor) < std::tie(b.major, b.minor);
    });
    if (steps.empty()) {
        fprintf(stderr, "[DB] no migration files in %s\n", selected.c_str());
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return false;
    }
    for (std::size_t i = 0; i < steps.size(); ++i) {
        if (steps[i].major != steps.front().major ||
            (i && steps[i - 1].major == steps[i].major &&
             steps[i - 1].minor == steps[i].minor)) {
            fprintf(stderr, "[DB] mixed/duplicate migration version in %s\n",
                    selected.c_str());
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
    }
    {
        sqlite3_stmt *check = nullptr;
        const char *sql = "SELECT filename,checksum FROM schema_migrations "
                          "WHERE major=?1 AND minor=?2";
        if (sqlite3_prepare_v2(write_db_, sql, -1, &check, nullptr) == SQLITE_OK) {
            for (const auto &step : steps) {
                sqlite3_reset(check);
                sqlite3_bind_int(check, 1, step.major);
                sqlite3_bind_int(check, 2, step.minor);
                if (sqlite3_step(check) != SQLITE_ROW) continue;
                const auto *filename = sqlite3_column_text(check, 0);
                const auto *checksum = sqlite3_column_text(check, 1);
                if (!filename || !checksum ||
                    step.path.filename().string() !=
                        reinterpret_cast<const char *>(filename) ||
                    step.checksum != reinterpret_cast<const char *>(checksum)) {
                    fprintf(stderr, "[DB] checksum mismatch for V%d.%d\n",
                            step.major, step.minor);
                    sqlite3_finalize(check);
                    flock(lock_fd, LOCK_UN);
                    ::close(lock_fd);
                    return false;
                }
            }
        }
        sqlite3_finalize(check);
    }
    if (!empty_database && current_major != steps.front().major) {
        fprintf(stderr, "[DB] major schema mismatch: V%d.%d vs V%d\n",
                current_major, current_minor, steps.front().major);
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return false;
    }
    if (empty_database && steps.front().major >= 1)
        sqlite3_exec(write_db_, "PRAGMA auto_vacuum=INCREMENTAL", nullptr,
                     nullptr, nullptr);
    if (!empty_database && current_minor > steps.back().minor) {
        fprintf(stderr, "[DB] warning: proxy V%d.%d is newer than known V%d.%d; "
                "continuing under same-major compatibility\n",
                current_major, current_minor, steps.back().major, steps.back().minor);
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return true;
    }

    auto sql_quote = [](const std::string &value) {
        std::string result = "'";
        for (char c : value) result += c == '\'' ? "''" : std::string(1, c);
        return result + "'";
    };

    bool ok = true;
    for (const auto &step : steps) {
        if (!empty_database && step.minor <= current_minor) continue;
        std::ifstream f(step.path);
        std::string body((std::istreambuf_iterator<char>(f)),
                         std::istreambuf_iterator<char>());
        if (body.empty()) {
            fprintf(stderr, "[DB] Migration %s is empty/unreadable\n",
                    step.path.c_str());
            ok = false;
            break;
        }
        const int user_version = step.major * 10000 + step.minor;
        std::string sql =
            "BEGIN IMMEDIATE;\n"
            "CREATE TABLE IF NOT EXISTS schema_version("
            "id INTEGER PRIMARY KEY CHECK(id=1),major INTEGER NOT NULL,"
            "minor INTEGER NOT NULL,database_name TEXT NOT NULL,updated_at TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "major INTEGER NOT NULL,minor INTEGER NOT NULL,filename TEXT NOT NULL,"
            "checksum TEXT NOT NULL,applied_at TEXT NOT NULL,PRIMARY KEY(major,minor),"
            "UNIQUE(filename));\n" + body +
            "\nINSERT INTO schema_migrations(major,minor,filename,checksum,applied_at) VALUES(" +
            std::to_string(step.major) + "," + std::to_string(step.minor) + "," +
            sql_quote(step.path.filename().string()) + "," + sql_quote(step.checksum) +
            ",strftime('%Y-%m-%dT%H:%M:%fZ','now'));"
            "INSERT INTO schema_version(id,major,minor,database_name,updated_at) VALUES(1," +
            std::to_string(step.major) + "," + std::to_string(step.minor) +
            ",'proxy',strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(id) DO UPDATE SET "
            "major=excluded.major,minor=excluded.minor,database_name=excluded.database_name,"
            "updated_at=excluded.updated_at;"
            "PRAGMA user_version=" + std::to_string(user_version) + ";\nCOMMIT;";

        char *err = nullptr;
        if (sqlite3_exec(write_db_, sql.c_str(), nullptr, nullptr, &err) != SQLITE_OK) {
            fprintf(stderr, "[DB] Migration %s failed: %s\n",
                    step.path.c_str(), err ? err : sqlite3_errmsg(write_db_));
            if (err) sqlite3_free(err);
            sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr);  // atomic step rollback
            ok = false;
            break;
        }
    }

    flock(lock_fd, LOCK_UN);
    ::close(lock_fd);
    return ok;
}


// ── Prepared statements ──────────────────────────────────────────────────

bool Database::prepare_statements() {
    bool ok = true;
    #define PREPARE_ON(conn, sql, stmt) \
        do { \
            int _rc = sqlite3_prepare_v2(conn, sql, -1, &stmt, nullptr); \
            if (_rc != SQLITE_OK) { \
                fprintf(stderr, "[DB] Prepare error: %s\n", sqlite3_errmsg(conn)); \
                if (stmt) sqlite3_finalize(stmt); \
                stmt = nullptr; \
                ok = false; \
            } \
        } while (0)

    if (schema_major_ == 1) {
        PREPARE_ON(read_db_,
            "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,'') "
            "FROM client_keys ck WHERE ck.key_value=?1 AND ck.enabled=1 "
            "AND (ck.deleted_at IS NULL OR ck.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            stmt_lookup_key_);
        PREPARE_ON(read_db_,
            "SELECT rs.id,rs.name,COALESCE(u.base_url,''),COALESCE(u.api_format,'openai'),"
            "COALESCE(u.endpoint_path,''),COALESCE(u.auth_scheme,'auto'),"
            "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,"
            "COALESCE(u.max_concurrency,0),CASE WHEN rs.enabled=1 THEN 0 ELSE 1 END "
            "FROM route_sets rs LEFT JOIN upstreams u ON u.account_id=rs.account_id "
            "AND u.enabled=1 WHERE rs.id=?1 ORDER BY u.id LIMIT 1",
            stmt_get_account_);
        PREPARE_ON(read_db_,
            "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,''),"
            "rs.id,rs.name,COALESCE(u.base_url,''),COALESCE(u.api_format,'openai'),"
            "COALESCE(u.endpoint_path,''),COALESCE(u.auth_scheme,'auto'),"
            "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,"
            "COALESCE(u.max_concurrency,0),0 "
            "FROM client_keys ck JOIN route_sets rs ON rs.id=ck.route_set_id "
            "LEFT JOIN upstreams u ON u.account_id=rs.account_id AND u.enabled=1 "
            "WHERE ck.key_value=?1 AND ck.enabled=1 AND rs.enabled=1 "
            "AND (ck.deleted_at IS NULL OR ck.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ORDER BY u.id LIMIT 1",
            stmt_lookup_route_);
        PREPARE_ON(read_db_,
            "SELECT c.runtime_id,s.secret_value,c.position FROM upstream_credentials c "
            "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?1 "
            "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ORDER BY c.position,c.runtime_id",
            stmt_get_upstream_keys_);
        PREPARE_ON(read_db_,
            "SELECT a.id,a.name,u.base_url,u.api_format,COALESCE(u.endpoint_path,''),"
            "COALESCE(u.auth_scheme,'auto'),0,"
            "CASE WHEN json_extract(COALESCE(bc.cooldown_policy_json,'{}'),'$.kind')="
            "'subscription_5h' THEN 1 ELSE 0 END,"
            "u.max_concurrency,0,COALESCE(rr.target_model,?2),rr.priority,"
            "c.runtime_id,s.secret_value,c.position "
            "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
            "LEFT JOIN accounts a ON a.id=u.account_id "
            "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
            "AND bc.valid_until IS NULL "
            "LEFT JOIN upstream_credentials c ON c.upstream_id=u.id "
            "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "WHERE rr.route_set_id=?1 AND rr.enabled=1 "
            "AND (rr.model_pattern='*' OR ?2 GLOB rr.model_pattern) "
            "AND u.enabled=1 ORDER BY rr.priority,rr.id,c.position,c.runtime_id",
            stmt_resolve_routing_snapshot_);
        PREPARE_ON(write_db_,
            "INSERT INTO request_log(event_id,source_kind,account_id,route_set_id,"
            "client_key_id,upstream_key_id,credential_uuid,model,prompt_tokens,completion_tokens,"
            "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,"
            "is_streaming,status_code,duration_ms,ttft_ms,generation_ms,output_tps,"
            "upstream_ttft_ms,upstream_duration_ms,attempt_count,fallback_count,requested_at) "
            "VALUES(?21,'proxy',(SELECT id FROM accounts WHERE id=?1),"
            "(SELECT route_set_id FROM client_keys WHERE id=?2),"
            "(SELECT id FROM client_keys WHERE id=?2),?12,"
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?12),"
            "?3,?4,?5,?6,?7,?8,CASE WHEN EXISTS(SELECT 1 FROM billing_contracts "
            "WHERE account_id=?1 AND charge_type='recurring' AND valid_until IS NULL) "
            "THEN 0 ELSE ?8 END,?9,?10,?11,COALESCE(?13,0),COALESCE(?14,0),"
            "COALESCE(?15,0),COALESCE(?16,0),COALESCE(?17,0),?18,?19,"
            "strftime('%Y-%m-%dT%H:%M:%fZ',?20,'unixepoch'))",
            stmt_insert_log_);
        PREPARE_ON(write_db_, "SELECT id FROM request_log WHERE event_id=?1",
                   stmt_find_log_event_);
        PREPARE_ON(write_db_,
            "INSERT INTO request_attempts(request_log_id,attempt_index,upstream_id,"
            "credential_uuid,account_id,upstream_key_id,status_code,duration_ms,ttft_ms,is_timeout,error,requested_at,"
            "dns_ms,connect_ms,tls_ms,lease_wait_ms) "
            "VALUES(?1,?2,(SELECT id FROM upstreams WHERE account_id=?3 ORDER BY id LIMIT 1),"
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?4),?3,?4,?5,?6,"
            "COALESCE(?7,0),?8,?9,strftime('%Y-%m-%dT%H:%M:%fZ',?10,'unixepoch'),"
            "?11,?12,?13,?14)",
            stmt_insert_attempt_);
        PREPARE_ON(read_db_,
            "SELECT model_pattern,u.account_id,COALESCE(target_model,model_pattern) "
            "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
            "WHERE route_set_id=?1 AND rr.enabled=1 ORDER BY priority,rr.id",
            stmt_get_aggregate_entries_);
        PREPARE_ON(read_db_,
            "SELECT pr.id,pr.model_pattern,r.input_price,r.output_price "
            "FROM pricing_rules pr JOIN pricing_rates r ON r.pricing_rule_id=pr.id "
            "WHERE pr.enabled=1 AND r.valid_until IS NULL ORDER BY pr.priority,pr.id",
            stmt_get_pricing_);
        PREPARE_ON(pricing_db_,
            "SELECT r.input_price,r.output_price,r.cache_read_price,"
            "COALESCE((SELECT ps.multiplier FROM pricing_slots ps WHERE ps.pricing_rate_id=r.id "
            "AND ((ps.start_minute<=ps.end_minute AND ?2>=ps.start_minute AND ?2<ps.end_minute) "
            "OR (ps.start_minute>ps.end_minute AND (?2>=ps.start_minute OR ?2<ps.end_minute))) "
            "ORDER BY ps.id LIMIT 1),1.0),r.currency,"
            "COALESCE((SELECT fx.rate FROM fx_rates fx WHERE fx.base_currency='USD' "
            "AND fx.quote_currency='CNY' AND fx.date<=date(?3,'unixepoch') "
            "ORDER BY fx.date DESC LIMIT 1),1.0) "
            "FROM pricing_rules pr JOIN pricing_rates r ON r.pricing_rule_id=pr.id "
            "WHERE pr.enabled=1 AND LOWER(?1) GLOB LOWER(pr.model_pattern) "
            "AND r.valid_from<=strftime('%Y-%m-%dT%H:%M:%fZ',?3,'unixepoch') "
            "AND (r.valid_until IS NULL OR r.valid_until>strftime('%Y-%m-%dT%H:%M:%fZ',?3,'unixepoch')) "
            "ORDER BY pr.priority,pr.id,r.valid_from DESC LIMIT 1",
            stmt_snapshot_price_);
        PREPARE_ON(read_db_,
            "SELECT streaming_first_byte_timeout,streaming_idle_timeout,"
            "non_streaming_timeout FROM proxy_timeout_config WHERE endpoint_kind="
            "CASE ?1 WHEN 'anthropic' THEN 'messages' WHEN 'openai_responses' "
            "THEN 'responses' ELSE 'chat' END",
            stmt_get_timeout_config_);
        PREPARE_ON(read_db_,
            "SELECT u.base_url,s.secret_value,u.api_format,u.auth_scheme "
            "FROM upstream_credentials c JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "JOIN upstreams u ON u.id=c.upstream_id WHERE c.runtime_id=?1 "
            "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND u.enabled=1",
            stmt_lookup_probe_target_);
        PREPARE_ON(write_db_,
            "UPDATE client_keys SET last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ',?2,'unixepoch') "
            "WHERE id=?1 AND (last_used_at IS NULL OR "
            "last_used_at<strftime('%Y-%m-%dT%H:%M:%fZ',?2,'unixepoch'))",
            stmt_update_last_used_);
        return ok;
    }

    PREPARE_ON(read_db_, "SELECT id, key_value, account_id, "
            "COALESCE(label,'') "
            "FROM local_keys WHERE key_value = ?1",
            stmt_lookup_key_);

    PREPARE_ON(read_db_, "SELECT id, name, base_url, api_format, "
            "COALESCE(endpoint_path,''), COALESCE(auth_header,'bearer'), "
            "COALESCE(is_aggregate,0), COALESCE(account_type,'api'), "
            "COALESCE(max_concurrency,0), "
            "(deleted_at IS NOT NULL AND deleted_at <= datetime('now')) AS deleted_at "
            "FROM upstream_accounts WHERE id = ?1",
            stmt_get_account_);

    {
        // Route filter excludes non-routable account types (see
        // account_types::routable_filter_sql — built once at startup).
        std::string route_sql =
            "SELECT k.id, k.key_value, k.account_id, COALESCE(k.label,''), "
            "a.id, a.name, a.base_url, a.api_format, "
            "COALESCE(a.endpoint_path,''), COALESCE(a.auth_header,'bearer'), "
            "COALESCE(a.is_aggregate,0), COALESCE(a.account_type,'api'), "
            "COALESCE(a.max_concurrency,0), "
            "(a.deleted_at IS NOT NULL AND a.deleted_at <= datetime('now')) AS a_deleted "
            "FROM local_keys k JOIN upstream_accounts a ON a.id=k.account_id "
            "WHERE k.key_value=?1"
            + account_types::routable_filter_sql("a.account_type")
            + " AND (a.deleted_at IS NULL OR a.deleted_at > datetime('now'))";
        PREPARE_ON(read_db_, route_sql.c_str(), stmt_lookup_route_);
    }

    PREPARE_ON(read_db_, "SELECT id, key_value, position "
            "FROM upstream_keys WHERE account_id = ?1 "
            "  AND (deleted_at IS NULL OR deleted_at > datetime('now')) "
            "ORDER BY position, id",
            stmt_get_upstream_keys_);

    // A single statement gives the account metadata, aggregate mapping and
    // key set one SQLite snapshot.  In particular, a concurrent config sync
    // cannot produce an old base_url paired with a newly committed key.
    {
        std::string snapshot_sql =
            "WITH root AS ("
            "  SELECT id, COALESCE(is_aggregate,0) AS is_aggregate "
            "  FROM upstream_accounts WHERE id=?1 "
            "    AND (deleted_at IS NULL OR deleted_at > datetime('now')) "
            + account_types::routable_filter_sql("account_type")
            + "), targets(target_id, upstream_model, priority_group, "
            "          priority_sort, priority_id) AS ("
            "  SELECT r.id, ?2, 0, 0, 0 FROM root r WHERE r.is_aggregate=0 "
            "  UNION ALL "
            "  SELECT e.upstream_account_id, e.upstream_model, e.id, "
            "         e.sort_order, e.id "
            "  FROM root r JOIN aggregate_entries e ON e.account_id=r.id "
            "  WHERE r.is_aggregate=1 AND e.pattern=?2"
            ") "
            "SELECT a.id, a.name, a.base_url, a.api_format, "
            "COALESCE(a.endpoint_path,''), COALESCE(a.auth_header,'bearer'), "
            "COALESCE(a.is_aggregate,0), COALESCE(a.account_type,'api'), "
            "COALESCE(a.max_concurrency,0), "
            "(a.deleted_at IS NOT NULL AND a.deleted_at <= datetime('now')) AS a_deleted, "
            "targets.upstream_model, targets.priority_group, "
            "k.id, k.key_value, k.position "
            "FROM targets JOIN upstream_accounts a "
            "  ON a.id=targets.target_id "
            " AND (a.deleted_at IS NULL OR a.deleted_at > datetime('now')) "
            + account_types::routable_filter_sql("a.account_type")
            + "LEFT JOIN upstream_keys k "
            "  ON k.account_id=a.id "
            " AND (k.deleted_at IS NULL OR k.deleted_at > datetime('now')) "
            "ORDER BY targets.priority_sort, targets.priority_id, "
            "         k.position, k.id";
        PREPARE_ON(read_db_, snapshot_sql.c_str(), stmt_resolve_routing_snapshot_);
    }

    PREPARE_ON(write_db_, "INSERT INTO request_log "
            "(account_id, local_key_id, model, prompt_tokens, "
            " completion_tokens, cache_read_tokens, total_tokens, api_cost, "
            " is_streaming, status_code, duration_ms, upstream_key_id, "
            " ttft_ms, generation_ms, output_tps, upstream_ttft_ms, "
            " upstream_duration_ms, attempt_count, fallback_count, requested_at, "
            " event_id, cost_frozen) "
            "VALUES ((SELECT id FROM upstream_accounts WHERE id=?1),"
            "(SELECT id FROM local_keys WHERE id=?2),"
            "?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,"
            "datetime(?20,'unixepoch'),?21,?22)",
            stmt_insert_log_);

    PREPARE_ON(write_db_,
            "SELECT id FROM request_log WHERE event_id=?1",
            stmt_find_log_event_);

    PREPARE_ON(write_db_, "INSERT INTO request_attempts "
            "(request_log_id, attempt_index, account_id, upstream_key_id, "
            " status_code, duration_ms, ttft_ms, is_timeout, error, requested_at) "
            "VALUES (?1,?2,(SELECT id FROM upstream_accounts WHERE id=?3),"
            "?4,?5,?6,?7,?8,?9,datetime(?10,'unixepoch'))",
            stmt_insert_attempt_);

    PREPARE_ON(read_db_, "SELECT pattern, upstream_account_id, upstream_model "
            "FROM aggregate_entries WHERE account_id = ?1 ORDER BY sort_order, id",
            stmt_get_aggregate_entries_);

    PREPARE_ON(read_db_, "SELECT id, model_pattern, input_price, output_price "
            "FROM model_pricing ORDER BY id",
            stmt_get_pricing_);

    PREPARE_ON(pricing_db_,
            "SELECT v.input_price, v.output_price, "
            "       v.cache_read_price, "
            "       COALESCE((SELECT ps.multiplier FROM pricing_slots ps "
            "                 WHERE ps.pricing_id=v.id AND "
            "                   ((ps.start_minute<=ps.end_minute AND "
            "                     ?2>=ps.start_minute AND ?2<ps.end_minute) OR "
            "                    (ps.start_minute>ps.end_minute AND "
            "                     (?2>=ps.start_minute OR ?2<ps.end_minute))) "
            "                 ORDER BY ps.id LIMIT 1), 1.0), "
            "       v.currency, "
            "       COALESCE((SELECT fr.rate FROM fx_rate fr "
            "                 WHERE fr.base='USD' AND fr.quote='CNY' "
            "                   AND fr.date <= date(?3,'unixepoch') "
            "                 ORDER BY fr.date DESC LIMIT 1), 1.0) "
            "FROM v_pricing_rate v "
            "WHERE LOWER(?1) GLOB LOWER(v.model_pattern) "
            "ORDER BY v.id LIMIT 1",
            stmt_snapshot_price_);

    PREPARE_ON(read_db_, "SELECT streaming_first_byte_timeout, streaming_idle_timeout, "
            "non_streaming_timeout FROM proxy_timeout_config WHERE app_type = ?1",
            stmt_get_timeout_config_);

    // Cooldown probe: one key slot → the upstream endpoint + that key's secret
    // + the auth scheme.  read_db_ carries a WAL busy timeout so a concurrent
    // checkpoint cannot spuriously fail a background probe.
    PREPARE_ON(read_db_,
            "SELECT a.base_url, k.key_value, "
            "COALESCE(a.api_format,'openai'), COALESCE(a.auth_header,'bearer') "
            "FROM upstream_keys k JOIN upstream_accounts a ON a.id=k.account_id "
            "WHERE k.id=?1 "
            "  AND (k.deleted_at IS NULL OR k.deleted_at > datetime('now')) "
            "  AND (a.deleted_at IS NULL OR a.deleted_at > datetime('now'))",
            stmt_lookup_probe_target_);

    PREPARE_ON(write_db_, "UPDATE local_keys "
            "SET last_used_at = datetime(?2,'unixepoch') "
            "WHERE id = ?1 AND (last_used_at IS NULL OR "
            "last_used_at < datetime(?2,'unixepoch'))",
            stmt_update_last_used_);

    #undef PREPARE_ON
    return ok;
}

void Database::finalize_statements() {
    #define FINALIZE(s) do { if (s) { sqlite3_finalize(s); s = nullptr; } } while (0)
    FINALIZE(stmt_lookup_key_);
    FINALIZE(stmt_get_account_);
    FINALIZE(stmt_lookup_route_);
    FINALIZE(stmt_get_upstream_keys_);
    FINALIZE(stmt_resolve_routing_snapshot_);
    FINALIZE(stmt_insert_log_);
    FINALIZE(stmt_find_log_event_);
    FINALIZE(stmt_insert_attempt_);
    FINALIZE(stmt_get_aggregate_entries_);
    FINALIZE(stmt_get_pricing_);
    FINALIZE(stmt_snapshot_price_);
    FINALIZE(stmt_get_timeout_config_);
    FINALIZE(stmt_lookup_probe_target_);
    FINALIZE(stmt_update_last_used_);
    #undef FINALIZE
}

// ── lookup_local_key ─────────────────────────────────────────────────────

std::optional<Database::KeyInfo> Database::lookup_local_key(
    const std::string &key_value) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

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
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_get_account_);
    sqlite3_bind_int(stmt_get_account_, 1, account_id);

    std::optional<AccountInfo> result;
    if (sqlite3_step(stmt_get_account_) == SQLITE_ROW) {
        AccountInfo info;
        info.id = sqlite3_column_int(stmt_get_account_, 0);
        info.name = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 1));
        info.base_url = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 2));
        info.api_format = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 3));
        if (info.api_format.empty()) info.api_format = "openai";
        info.endpoint_path = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 4));
        info.auth_header = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 5));
        if (info.auth_header.empty()) info.auth_header = "bearer";
        info.is_aggregate = sqlite3_column_int(stmt_get_account_, 6) != 0;
        if (schema_major_ == 1) {
            info.extended_usage_limit_cooldown =
                sqlite3_column_int(stmt_get_account_, 7) != 0;
        } else {
            const char *atype = reinterpret_cast<const char *>(
                sqlite3_column_text(stmt_get_account_, 7));
            info.account_type = atype ? atype : "api";
            info.extended_usage_limit_cooldown =
                account_types::cooldown_class(info.account_type) ==
                account_types::CooldownClass::kSubscription5h;
        }
        info.max_concurrency = sqlite3_column_int(stmt_get_account_, 8);
        info.deleted = sqlite3_column_int(stmt_get_account_, 9) != 0;
        result = std::move(info);
    }
    sqlite3_reset(stmt_get_account_);
    return result;
}

std::optional<Database::RouteInfo> Database::lookup_route(
    const std::string &key_value) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_reset(stmt_lookup_route_);
    sqlite3_bind_text(stmt_lookup_route_, 1, key_value.c_str(),
                      key_value.size(), SQLITE_STATIC);
    std::optional<RouteInfo> result;
    if (sqlite3_step(stmt_lookup_route_) == SQLITE_ROW) {
        RouteInfo route;
        route.key.id = sqlite3_column_int(stmt_lookup_route_, 0);
        route.key.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_route_, 1));
        route.key.account_id = sqlite3_column_int(stmt_lookup_route_, 2);
        route.key.label = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_route_, 3));
        auto &a = route.account;
        a.id = sqlite3_column_int(stmt_lookup_route_, 4);
        a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 5));
        a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 6));
        a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 7));
        a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 8));
        a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 9));
        a.is_aggregate = sqlite3_column_int(stmt_lookup_route_, 10) != 0;
        if (schema_major_ == 1) {
            a.extended_usage_limit_cooldown =
                sqlite3_column_int(stmt_lookup_route_, 11) != 0;
        } else {
            const char *atype = reinterpret_cast<const char *>(
                sqlite3_column_text(stmt_lookup_route_, 11));
            a.account_type = atype ? atype : "api";
            a.extended_usage_limit_cooldown =
                account_types::cooldown_class(a.account_type) ==
                account_types::CooldownClass::kSubscription5h;
        }
        a.max_concurrency = sqlite3_column_int(stmt_lookup_route_, 12);
        a.deleted = sqlite3_column_int(stmt_lookup_route_, 13) != 0;
        result = std::move(route);
    }
    sqlite3_reset(stmt_lookup_route_);
    return result;
}

std::uint64_t Database::routing_config_generation() {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_stmt *stmt = nullptr;
    std::uint64_t generation = 0;
    const char *sql = schema_major_ == 1
        ? "SELECT generation FROM config_state WHERE id=1"
        : "PRAGMA data_version";
    if (sqlite3_prepare_v2(read_db_, sql, -1, &stmt,
                           nullptr) == SQLITE_OK &&
        sqlite3_step(stmt) == SQLITE_ROW)
        generation = static_cast<std::uint64_t>(sqlite3_column_int64(stmt, 0));
    sqlite3_finalize(stmt);
    return generation;
}

bool Database::load_routing_config(RoutingConfig &config) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    RoutingConfig next;
    char *error = nullptr;
    if (sqlite3_exec(read_db_, "BEGIN", nullptr, nullptr, &error) != SQLITE_OK) {
        fprintf(stderr, "[DB] routing snapshot begin failed: %s\n",
                error ? error : sqlite3_errmsg(read_db_));
        if (error) sqlite3_free(error);
        return false;
    }
    ReadTransactionGuard rollback(read_db_);

    sqlite3_stmt *stmt = nullptr;
    const char *generation_sql = schema_major_ == 1
        ? "SELECT generation FROM config_state WHERE id=1"
        : "PRAGMA data_version";
    if (sqlite3_prepare_v2(read_db_, generation_sql, -1, &stmt,
                           nullptr) == SQLITE_OK &&
        sqlite3_step(stmt) == SQLITE_ROW)
        next.generation = static_cast<std::uint64_t>(
            sqlite3_column_int64(stmt, 0));
    sqlite3_finalize(stmt);

    const std::string routes_sql_v0 =
        "SELECT k.id,k.key_value,k.account_id,COALESCE(k.label,''),"
        "a.id,a.name,a.base_url,COALESCE(a.api_format,'openai'),"
        "COALESCE(a.endpoint_path,''),COALESCE(a.auth_header,'auto'),"
        "COALESCE(a.is_aggregate,0),COALESCE(a.account_type,'api'),"
        "COALESCE(a.max_concurrency,0),0 "
        "FROM local_keys k JOIN upstream_accounts a ON a.id=k.account_id "
        "WHERE (a.deleted_at IS NULL OR a.deleted_at>datetime('now'))" +
        account_types::routable_filter_sql("a.account_type") +
        " ORDER BY k.id";
    const std::string routes_sql_v1 =
        "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,''),"
        "rs.id,rs.name,'','openai','','auto',"
        "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,0,0 "
        "FROM client_keys ck JOIN route_sets rs ON rs.id=ck.route_set_id "
        "WHERE ck.enabled=1 AND rs.enabled=1 "
        "AND (ck.deleted_at IS NULL OR ck.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ORDER BY ck.id";
    const std::string &routes_sql = schema_major_ == 1
        ? routes_sql_v1 : routes_sql_v0;
    if (sqlite3_prepare_v2(read_db_, routes_sql.c_str(), -1, &stmt,
                           nullptr) != SQLITE_OK) {
        fprintf(stderr, "[DB] routing snapshot routes failed: %s\n",
                sqlite3_errmsg(read_db_));
        return false;
    }
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        RouteInfo route;
        route.key.id = sqlite3_column_int(stmt, 0);
        route.key.key_value = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 1));
        route.key.account_id = sqlite3_column_int(stmt, 2);
        route.key.label = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 3));
        auto &a = route.account;
        a.id = sqlite3_column_int(stmt, 4);
        a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 5));
        a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 6));
        a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 7));
        a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 8));
        a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 9));
        a.is_aggregate = sqlite3_column_int(stmt, 10) != 0;
        if (schema_major_ == 1) {
            a.extended_usage_limit_cooldown = sqlite3_column_int(stmt, 11) != 0;
        } else {
            a.account_type = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 11));
            a.extended_usage_limit_cooldown =
                account_types::cooldown_class(a.account_type) ==
                account_types::CooldownClass::kSubscription5h;
        }
        a.max_concurrency = sqlite3_column_int(stmt, 12);
        a.deleted = false;
        next.routes.push_back(std::move(route));
    }
    sqlite3_finalize(stmt);
    stmt = nullptr;

    const std::string rules_sql_v0 =
        "WITH rules(route_set_id,model_pattern,target_id,target_model,"
        "priority_sort,priority_group) AS ("
        " SELECT id,'*',id,NULL,0,0 FROM upstream_accounts "
        " WHERE COALESCE(is_aggregate,0)=0" +
        account_types::routable_filter_sql("account_type") +
        " UNION ALL "
        " SELECT e.account_id,e.pattern,e.upstream_account_id,e.upstream_model,"
        "        e.sort_order,e.id FROM aggregate_entries e"
        ") "
        "SELECT r.route_set_id,r.model_pattern,r.target_model,r.priority_group,"
        "a.id,a.name,a.base_url,COALESCE(a.api_format,'openai'),"
        "COALESCE(a.endpoint_path,''),COALESCE(a.auth_header,'auto'),"
        "COALESCE(a.account_type,'api'),COALESCE(a.max_concurrency,0),"
        "k.id,k.key_value,k.position "
        "FROM rules r JOIN upstream_accounts a ON a.id=r.target_id "
        "LEFT JOIN upstream_keys k ON k.account_id=a.id "
        " AND (k.deleted_at IS NULL OR k.deleted_at>datetime('now')) "
        "WHERE (a.deleted_at IS NULL OR a.deleted_at>datetime('now'))" +
        account_types::routable_filter_sql("a.account_type") +
        " ORDER BY r.route_set_id,r.priority_sort,r.priority_group,k.position,k.id";
    const std::string rules_sql_v1 =
        "SELECT rr.route_set_id,rr.model_pattern,rr.target_model,rr.priority,"
        "a.id,a.name,u.base_url,u.api_format,COALESCE(u.endpoint_path,''),"
        "COALESCE(u.auth_scheme,'auto'),"
        "CASE WHEN json_extract(COALESCE(bc.cooldown_policy_json,'{}'),'$.kind')="
        "'subscription_5h' THEN 1 ELSE 0 END,"
        "u.max_concurrency,c.runtime_id,s.secret_value,c.position "
        "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
        "JOIN accounts a ON a.id=u.account_id "
        "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
        "AND bc.valid_until IS NULL "
        "LEFT JOIN upstream_credentials c ON c.upstream_id=u.id "
        "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
        "WHERE rr.enabled=1 AND u.enabled=1 AND a.lifecycle_state='active' "
        "ORDER BY rr.route_set_id,rr.priority,rr.id,c.position,c.runtime_id";
    const std::string &rules_sql = schema_major_ == 1
        ? rules_sql_v1 : rules_sql_v0;
    if (sqlite3_prepare_v2(read_db_, rules_sql.c_str(), -1, &stmt,
                           nullptr) != SQLITE_OK) {
        fprintf(stderr, "[DB] routing snapshot rules failed: %s\n",
                sqlite3_errmsg(read_db_));
        return false;
    }
    int last_route = -1;
    int last_group = -1;
    int last_target = -1;
    std::string last_pattern;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        const int route_id = sqlite3_column_int(stmt, 0);
        const char *pattern_text = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 1));
        const std::string pattern = pattern_text ? pattern_text : "*";
        const int group = sqlite3_column_int(stmt, 3);
        const int target_id = sqlite3_column_int(stmt, 4);
        if (next.rules.empty() || route_id != last_route || group != last_group ||
            target_id != last_target || pattern != last_pattern) {
            RoutingRule rule;
            rule.route_set_id = route_id;
            rule.model_pattern = pattern;
            const unsigned char *target_model = sqlite3_column_text(stmt, 2);
            rule.target.upstream_model = target_model
                ? reinterpret_cast<const char *>(target_model) : "";
            rule.target.priority_group = group;
            auto &a = rule.target.account;
            a.id = target_id;
            a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 5));
            a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 6));
            a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 7));
            a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 8));
            a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 9));
            if (schema_major_ == 1) {
                a.extended_usage_limit_cooldown = sqlite3_column_int(stmt, 10) != 0;
            } else {
                a.account_type = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 10));
                a.extended_usage_limit_cooldown =
                    account_types::cooldown_class(a.account_type) ==
                    account_types::CooldownClass::kSubscription5h;
            }
            a.max_concurrency = sqlite3_column_int(stmt, 11);
            a.is_aggregate = false;
            a.deleted = false;
            next.rules.push_back(std::move(rule));
            last_route = route_id;
            last_group = group;
            last_target = target_id;
            last_pattern = pattern;
        }
        if (sqlite3_column_type(stmt, 12) != SQLITE_NULL) {
            KeySlot key;
            key.id = sqlite3_column_int(stmt, 12);
            key.key_value = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 13));
            key.position = sqlite3_column_int(stmt, 14);
            next.rules.back().target.keys.push_back(std::move(key));
        }
    }
    sqlite3_finalize(stmt);
    if (sqlite3_exec(read_db_, "COMMIT", nullptr, nullptr, &error) != SQLITE_OK) {
        fprintf(stderr, "[DB] routing snapshot commit failed: %s\n",
                error ? error : sqlite3_errmsg(read_db_));
        if (error) sqlite3_free(error);
        return false;
    }
    rollback.release();
    config = std::move(next);
    return true;
}

// ── get_upstream_keys ────────────────────────────────────────────────────

std::vector<Database::KeySlot> Database::get_upstream_keys(int account_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_get_upstream_keys_);
    sqlite3_bind_int(stmt_get_upstream_keys_, 1, account_id);

    std::vector<KeySlot> result;
    while (sqlite3_step(stmt_get_upstream_keys_) == SQLITE_ROW) {
        KeySlot k;
        k.id = sqlite3_column_int(stmt_get_upstream_keys_, 0);
        k.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_upstream_keys_, 1));
        k.position = sqlite3_column_int(stmt_get_upstream_keys_, 2);
        result.push_back(std::move(k));
    }
    sqlite3_reset(stmt_get_upstream_keys_);
    return result;
}

// ── lookup_probe_target ──────────────────────────────────────────────────

std::optional<Database::ProbeTarget> Database::lookup_probe_target(
    int key_slot_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_reset(stmt_lookup_probe_target_);
    sqlite3_bind_int(stmt_lookup_probe_target_, 1, key_slot_id);
    ProbeTarget t;
    if (sqlite3_step(stmt_lookup_probe_target_) == SQLITE_ROW) {
        t.base_url = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 0));
        t.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 1));
        t.api_format = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 2));
        t.auth_header = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 3));
        t.valid = !t.base_url.empty() && !t.key_value.empty();
    }
    sqlite3_reset(stmt_lookup_probe_target_);
    if (!t.valid) return std::nullopt;
    return t;
}

// ── resolve_routing_snapshot ────────────────────────────────────────────

std::vector<Database::RoutingTarget> Database::resolve_routing_snapshot(
    int account_id, const std::string &model) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_resolve_routing_snapshot_);
    sqlite3_bind_int(stmt_resolve_routing_snapshot_, 1, account_id);
    sqlite3_bind_text(stmt_resolve_routing_snapshot_, 2, model.c_str(),
                      static_cast<int>(model.size()), SQLITE_STATIC);

    auto text_column = [this](int column) -> std::string {
        const auto *value = sqlite3_column_text(
            stmt_resolve_routing_snapshot_, column);
        return value ? reinterpret_cast<const char *>(value) : std::string();
    };

    std::vector<RoutingTarget> result;
    int rc = SQLITE_OK;
    while ((rc = sqlite3_step(stmt_resolve_routing_snapshot_)) == SQLITE_ROW) {
        const int target_id = sqlite3_column_int(
            stmt_resolve_routing_snapshot_, 0);
        const std::string upstream_model = text_column(10);
        const int priority_group = sqlite3_column_int(
            stmt_resolve_routing_snapshot_, 11);

        if (result.empty() || result.back().account.id != target_id ||
            result.back().priority_group != priority_group ||
            result.back().upstream_model != upstream_model) {
            RoutingTarget target;
            auto &account = target.account;
            account.id = target_id;
            account.name = text_column(1);
            account.base_url = text_column(2);
            account.api_format = text_column(3);
            if (account.api_format.empty()) account.api_format = "openai";
            account.endpoint_path = text_column(4);
            account.auth_header = text_column(5);
            if (account.auth_header.empty()) account.auth_header = "bearer";
            account.is_aggregate = sqlite3_column_int(
                stmt_resolve_routing_snapshot_, 6) != 0;
            if (schema_major_ == 1) {
                account.extended_usage_limit_cooldown =
                    sqlite3_column_int(stmt_resolve_routing_snapshot_, 7) != 0;
            } else {
                account.account_type = text_column(7);
                if (account.account_type.empty()) account.account_type = "api";
                account.extended_usage_limit_cooldown =
                    account_types::cooldown_class(account.account_type) ==
                    account_types::CooldownClass::kSubscription5h;
            }
            account.max_concurrency = sqlite3_column_int(
                stmt_resolve_routing_snapshot_, 8);
            account.deleted = sqlite3_column_int(
                stmt_resolve_routing_snapshot_, 9) != 0;
            target.upstream_model = upstream_model;
            target.priority_group = priority_group;
            result.push_back(std::move(target));
        }

        // LEFT JOIN preserves an account with no multi-key rows (keys is empty;
        // the single upstream_keys table is the only key source).
        if (sqlite3_column_type(stmt_resolve_routing_snapshot_, 12) !=
            SQLITE_NULL) {
            KeySlot key;
            key.id = sqlite3_column_int(stmt_resolve_routing_snapshot_, 12);
            key.key_value = text_column(13);
            key.position = sqlite3_column_int(
                stmt_resolve_routing_snapshot_, 14);
            result.back().keys.push_back(std::move(key));
        }
    }

    if (rc != SQLITE_DONE) {
        fprintf(stderr, "[DB] routing snapshot query error: %s\n",
                sqlite3_errmsg(read_db_));
        result.clear();
    }
    sqlite3_reset(stmt_resolve_routing_snapshot_);
    return result;
}

// ── log_request ──────────────────────────────────────────────────────────

std::string Database::serialize_log_record(const LogRecord &record) {
    json encoded_attempts = json::array();
    for (const auto &attempt : record.attempts) {
        encoded_attempts.push_back({
            {"account_id", attempt.account_id},
            {"upstream_key_id", attempt.upstream_key_id},
            {"status_code", attempt.status_code},
            {"duration_ms", attempt.duration_ms},
            {"dns_ms", attempt.dns_ms},
            {"connect_ms", attempt.connect_ms},
            {"tls_ms", attempt.tls_ms},
            {"lease_wait_ms", attempt.lease_wait_ms},
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
        {"cost_frozen", record.cost_frozen},
        {"is_streaming", record.is_streaming},
        {"status_code", record.status_code},
        {"duration_ms", record.duration_ms},
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
        record.cost_frozen = value.value("cost_frozen", false);
        record.is_streaming = value.value("is_streaming", false);
        record.status_code = value.value("status_code", 0);
        record.duration_ms = value.value("duration_ms", 0);
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
                attempt.upstream_key_id = encoded.value("upstream_key_id", 0);
                attempt.status_code = encoded.value("status_code", 0);
                attempt.duration_ms = encoded.value("duration_ms", 0);
                attempt.dns_ms = encoded.value("dns_ms", 0);
                attempt.connect_ms = encoded.value("connect_ms", 0);
                attempt.tls_ms = encoded.value("tls_ms", 0);
                attempt.lease_wait_ms = encoded.value("lease_wait_ms", 0);
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

bool Database::snapshot_request_cost(const std::string &model,
                                     int prompt_tokens,
                                     int completion_tokens,
                                     int cache_read_tokens,
                                     std::int64_t requested_at_unix,
                                     double &cost) {
    int minute = static_cast<int>((requested_at_unix % 86400 + 86400) % 86400) / 60;
    // Include the UTC date in the cache key: the FX rate is per-day, so a
    // snapshot fetched yesterday must never be reused for today's requests.
    std::tm tm{};
    const std::time_t ts = static_cast<std::time_t>(requested_at_unix);
    gmtime_r(&ts, &tm);
    char date_buf[16] = {0};
    std::strftime(date_buf, sizeof(date_buf), "%Y-%m-%d", &tm);
    const std::string cache_key = model + '\x1f' + std::string(date_buf) + '\x1f'
                                + std::to_string(minute);
    std::lock_guard<std::mutex> lock(pricing_mutex_);
    if (!pricing_db_ || !stmt_snapshot_price_) return false;

    const auto compute_cost = [&](const FrozenRate &rate) {
        if (!rate.matched) {
            cost = 0.0;
            return true;
        }
        const double prompt = std::max(0, prompt_tokens);
        const double completion = std::max(0, completion_tokens);
        const double cache = std::max(0, cache_read_tokens);
        const double uncached = std::max(0.0, prompt - cache);
        cost = ((uncached / 1000000.0) * rate.input_price +
                (cache / 1000000.0) * rate.cache_read_price +
                (completion / 1000000.0) * rate.output_price) * rate.multiplier;
        // USD-priced models are normalized to CNY using the rate in effect on
        // the request's UTC day (nearest-latest; 1.0 when none is stored).
        if (rate.currency == "USD") cost *= rate.fx_rate;
        return std::isfinite(cost);
    };

    // Cache first: within one minute-of-day the frozen rate is constant, so a
    // hit avoids the synchronous pricing SELECT entirely.  Only a miss pays
    // the SQLite read (and refills the cache for the rest of that minute).
    const auto cached = frozen_rate_cache_.find(cache_key);
    if (cached != frozen_rate_cache_.end())
        return compute_cost(cached->second);

    sqlite3_reset(stmt_snapshot_price_);
    sqlite3_bind_text(stmt_snapshot_price_, 1, model.c_str(),
                      static_cast<int>(model.size()), SQLITE_STATIC);
    sqlite3_bind_int(stmt_snapshot_price_, 2, minute);
    sqlite3_bind_int64(stmt_snapshot_price_, 3, requested_at_unix);

    FrozenRate rate;
    const int rc = sqlite3_step(stmt_snapshot_price_);
    if (rc == SQLITE_ROW) {
        rate.matched = true;
        rate.input_price = sqlite3_column_double(stmt_snapshot_price_, 0);
        rate.output_price = sqlite3_column_double(stmt_snapshot_price_, 1);
        rate.cache_read_price = sqlite3_column_double(stmt_snapshot_price_, 2);
        rate.multiplier = sqlite3_column_double(stmt_snapshot_price_, 3);
        const unsigned char *cur = sqlite3_column_text(stmt_snapshot_price_, 4);
        if (cur) rate.currency.assign(reinterpret_cast<const char *>(cur),
                                      static_cast<size_t>(sqlite3_column_bytes(stmt_snapshot_price_, 4)));
        rate.fx_rate = sqlite3_column_double(stmt_snapshot_price_, 5);
        sqlite3_reset(stmt_snapshot_price_);
        if (frozen_rate_cache_.size() >= 1024 &&
            frozen_rate_cache_.find(cache_key) == frozen_rate_cache_.end())
            frozen_rate_cache_.erase(frozen_rate_cache_.begin());
        frozen_rate_cache_[cache_key] = rate;
    } else if (rc == SQLITE_DONE) {
        sqlite3_reset(stmt_snapshot_price_);
        if (frozen_rate_cache_.size() >= 1024 &&
            frozen_rate_cache_.find(cache_key) == frozen_rate_cache_.end())
            frozen_rate_cache_.erase(frozen_rate_cache_.begin());
        frozen_rate_cache_[cache_key] = rate;  // explicit no-match snapshot
    } else {
        const std::string error = sqlite3_errmsg(pricing_db_);
        sqlite3_reset(stmt_snapshot_price_);
        const auto cached_on_error = frozen_rate_cache_.find(cache_key);
        if (cached_on_error == frozen_rate_cache_.end()) {
            fprintf(stderr,
                    "[DB] pricing snapshot unavailable and no immutable cache "
                    "exists (model=%s): %s\n",
                    model.c_str(), error.c_str());
            return false;
        }
        rate = cached_on_error->second;
        fprintf(stderr,
                "[DB] pricing snapshot read failed; using last immutable "
                "snapshot (model=%s): %s\n",
                model.c_str(), error.c_str());
    }

    return compute_cost(rate);
}

bool Database::append_log_spool_locked(const std::string &payload) {
    if (log_spool_fd_ < 0 || payload.empty() ||
        payload.size() > kLogRecordMaxBytes)
        return false;

    std::string frame;
    frame.reserve(kSpoolHeaderBytes + payload.size());
    append_u32_le(frame, static_cast<std::uint32_t>(payload.size()));
    append_u32_le(frame, spool_checksum(payload.data(), payload.size()));
    frame.append(payload);

    // Bound total spool size so an unavailable writer cannot grow the file
    // without limit.  Rejected records are lost (counted by log_request's
    // failure counter) but the disk is protected.
    if (log_spool_write_offset_ + frame.size() > kLogSpoolHardLimit) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr,
                "[DB] request-log spool hard limit reached (%llu bytes); "
                "dropping new records\n",
                static_cast<unsigned long long>(log_spool_write_offset_));
        return false;
    }

    const std::uint64_t original_offset = log_spool_write_offset_;
    if (!write_exact(log_spool_fd_, frame.data(), frame.size())) {
        const int saved_errno = errno;
        if (::ftruncate(log_spool_fd_, static_cast<off_t>(original_offset)) != 0)
            fprintf(stderr, "[DB] CRITICAL: failed to remove torn spool tail: %s\n",
                    std::strerror(errno));
        errno = saved_errno;
        fprintf(stderr, "[DB] request-log spool append error: %s\n",
                std::strerror(errno));
        return false;
    }
    log_spool_write_offset_ += frame.size();
    return true;
}

bool Database::read_log_spool_batch_locked(std::vector<SpoolRecord> &batch) {
    batch.clear();
    std::uint64_t offset = log_spool_read_offset_;
    std::size_t batch_bytes = 0;
    while (offset < log_spool_write_offset_ && batch.size() < kLogBatchSize) {
        char header[kSpoolHeaderBytes];
        if (!pread_exact(log_spool_fd_, header, sizeof(header), offset)) return false;
        const std::uint32_t payload_size = read_u32_le(header);
        const std::uint32_t checksum = read_u32_le(header + 4);
        if (payload_size == 0 || payload_size > kLogRecordMaxBytes) return false;
        const std::size_t frame_size = kSpoolHeaderBytes + payload_size;
        if (!batch.empty() && batch_bytes + frame_size > kLogBatchBytes) break;
        if (offset + frame_size > log_spool_write_offset_) return false;

        std::string payload(payload_size, '\0');
        if (!pread_exact(log_spool_fd_, payload.data(), payload.size(),
                         offset + kSpoolHeaderBytes) ||
            spool_checksum(payload.data(), payload.size()) != checksum)
            return false;

        SpoolRecord item;
        if (!deserialize_log_record(payload, item.record)) return false;
        offset += frame_size;
        item.end_offset = offset;
        item.frame_bytes = frame_size;
        batch_bytes += frame_size;
        batch.push_back(std::move(item));
    }
    return !batch.empty();
}

bool Database::compact_log_spool_locked(bool force) {
    if (log_spool_fd_ < 0 ||
        log_spool_read_offset_ != log_spool_write_offset_)
        return false;
    if (!force && log_spool_write_offset_ < kLogCompactThreshold) return true;
    if (::ftruncate(log_spool_fd_, 0) != 0) {
        fprintf(stderr, "[DB] request-log spool truncate error: %s\n",
                std::strerror(errno));
        return false;
    }
    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = 0;
    if (::fdatasync(log_spool_fd_) != 0) {
        fprintf(stderr, "[DB] request-log spool truncate sync error: %s\n",
                std::strerror(errno));
        return false;
    }
    return true;
}

bool Database::start_log_writer() {
    std::unique_lock<std::mutex> lock(log_queue_mutex_);
    if (log_writer_thread_.joinable()) {
        fprintf(stderr, "[DB] request-log writer is already running\n");
        return false;
    }

    const std::string spool_path = db_path_ + ".request-log.spool";
    log_spool_fd_ = ::open(spool_path.c_str(),
                           O_CREAT | O_RDWR | O_APPEND | O_CLOEXEC, 0600);
    if (log_spool_fd_ < 0) {
        fprintf(stderr, "[DB] cannot open request-log spool %s: %s\n",
                spool_path.c_str(), std::strerror(errno));
        return false;
    }
    if (::fchmod(log_spool_fd_, 0600) != 0) {
        fprintf(stderr, "[DB] cannot secure request-log spool permissions: %s\n",
                std::strerror(errno));
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }
    if (flock(log_spool_fd_, LOCK_EX | LOCK_NB) != 0) {
        fprintf(stderr,
                "[DB] request-log spool is already owned by another proxy: %s\n",
                std::strerror(errno));
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }

    struct stat st {};
    if (::fstat(log_spool_fd_, &st) != 0 || st.st_size < 0) {
        fprintf(stderr, "[DB] cannot stat request-log spool: %s\n",
                std::strerror(errno));
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        return false;
    }

    const std::uint64_t file_size = static_cast<std::uint64_t>(st.st_size);
    std::uint64_t valid_end = 0;
    while (valid_end + kSpoolHeaderBytes <= file_size) {
        char header[kSpoolHeaderBytes];
        if (!pread_exact(log_spool_fd_, header, sizeof(header), valid_end)) break;
        const std::uint32_t payload_size = read_u32_le(header);
        const std::uint32_t checksum = read_u32_le(header + 4);
        if (payload_size == 0 || payload_size > kLogRecordMaxBytes ||
            valid_end + kSpoolHeaderBytes + payload_size > file_size)
            break;
        std::string payload(payload_size, '\0');
        if (!pread_exact(log_spool_fd_, payload.data(), payload.size(),
                         valid_end + kSpoolHeaderBytes) ||
            spool_checksum(payload.data(), payload.size()) != checksum) break;
        LogRecord probe;
        if (!deserialize_log_record(payload, probe)) break;
        valid_end += kSpoolHeaderBytes + payload_size;
    }
    if (valid_end != file_size) {
        fprintf(stderr,
                "[DB] trimming %llu byte(s) from an incomplete/corrupt spool tail\n",
                static_cast<unsigned long long>(file_size - valid_end));
        if (::ftruncate(log_spool_fd_, static_cast<off_t>(valid_end)) != 0 ||
            ::fdatasync(log_spool_fd_) != 0) {
            fprintf(stderr, "[DB] failed to repair request-log spool: %s\n",
                    std::strerror(errno));
            flock(log_spool_fd_, LOCK_UN);
            ::close(log_spool_fd_);
            log_spool_fd_ = -1;
            return false;
        }
    }

    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = valid_end;
    log_stop_ = false;
    log_accepting_ = true;
    log_persist_failures_.store(0, std::memory_order_relaxed);
    try {
        log_writer_thread_ = std::thread(&Database::log_writer_loop, this);
    } catch (const std::exception &e) {
        log_accepting_ = false;
        log_stop_ = true;
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        fprintf(stderr, "[DB] request-log writer start error: %s\n", e.what());
        return false;
    } catch (...) {
        log_accepting_ = false;
        log_stop_ = true;
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
        fprintf(stderr, "[DB] request-log writer start error: unknown exception\n");
        return false;
    }
    if (valid_end != 0) log_queue_cv_.notify_one();
    return true;
}

void Database::stop_log_writer() {
    std::unique_lock<std::mutex> lock(log_queue_mutex_);
    log_accepting_ = false;
    log_stop_ = true;
    log_queue_cv_.notify_all();
    const bool joinable = log_writer_thread_.joinable();
    lock.unlock();
    if (joinable) log_writer_thread_.join();

    lock.lock();
    if (log_spool_read_offset_ != log_spool_write_offset_) {
        const auto pending = log_spool_write_offset_ - log_spool_read_offset_;
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr,
                "[DB] CRITICAL: shutdown retained %llu byte(s) of durable "
                "request-log spool for next startup\n",
                static_cast<unsigned long long>(pending));
    }
    if (log_spool_fd_ >= 0) {
        flock(log_spool_fd_, LOCK_UN);
        ::close(log_spool_fd_);
        log_spool_fd_ = -1;
    }
    log_spool_read_offset_ = 0;
    log_spool_write_offset_ = 0;
    log_stop_ = false;
}

void Database::log_writer_loop() {
    try {
        std::vector<SpoolRecord> batch;
        batch.reserve(kLogBatchSize);
        for (;;) {
            {
                std::unique_lock<std::mutex> lock(log_queue_mutex_);
                log_queue_cv_.wait(lock, [this] {
                    return log_stop_ ||
                           log_spool_read_offset_ < log_spool_write_offset_;
                });
                if (log_spool_read_offset_ == log_spool_write_offset_) {
                    if (log_stop_) {
                        compact_log_spool_locked(true);
                        break;
                    }
                    continue;
                }
                if (!read_log_spool_batch_locked(batch)) {
                    fprintf(stderr,
                            "[DB] CRITICAL: cannot decode durable request-log "
                            "spool at offset %llu\n",
                            static_cast<unsigned long long>(log_spool_read_offset_));
                    log_persist_failures_.fetch_add(1,
                                                    std::memory_order_relaxed);
                    if (log_stop_) break;
                    log_queue_cv_.wait_for(lock, std::chrono::seconds(1));
                    continue;
                }
            }

            std::vector<LogRecord> records;
            records.reserve(batch.size());
            for (auto &item : batch) records.push_back(std::move(item.record));

            int shutdown_failures = 0;
            int retry = 0;
            bool spool_synced = false;
            for (;;) {
                if (!spool_synced) {
                    int sync_rc;
                    do {
                        sync_rc = ::fdatasync(log_spool_fd_);
                    } while (sync_rc != 0 && errno == EINTR);
                    spool_synced = sync_rc == 0;
                    if (!spool_synced)
                        fprintf(stderr,
                                "[DB] request-log spool batch sync error: %s\n",
                                std::strerror(errno));
                }
                if (spool_synced &&
                    persist_log_records(records.data(), records.size()))
                    break;
                ++retry;
                bool stopping = false;
                {
                    std::unique_lock<std::mutex> lock(log_queue_mutex_);
                    stopping = log_stop_;
                    if (stopping && ++shutdown_failures >= kShutdownRetryLimit) {
                        log_persist_failures_.fetch_add(
                            records.size(), std::memory_order_relaxed);
                        fprintf(stderr,
                                "[DB] CRITICAL: SQLite unavailable during "
                                "shutdown; durable spool retained\n");
                        return;
                    }
                    const int backoff_ms = std::min(1000, 25 << std::min(retry, 5));
                    log_queue_cv_.wait_for(
                        lock, std::chrono::milliseconds(backoff_ms),
                        [this] { return log_stop_; });
                }
            }

            {
                std::lock_guard<std::mutex> lock(log_queue_mutex_);
                log_spool_read_offset_ = batch.back().end_offset;
                if (log_spool_read_offset_ == log_spool_write_offset_)
                    compact_log_spool_locked(log_stop_);
            }
        }
    } catch (const std::exception &e) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr,
                "[DB] CRITICAL: request-log writer exception; spool retained: %s\n",
                e.what());
        // The writer thread is dead; stop accepting new spool records so the
        // spool cannot grow unbounded with nothing draining it.
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
    } catch (...) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr,
                "[DB] CRITICAL: unknown request-log writer exception; spool retained\n");
        {
            std::lock_guard<std::mutex> lock(log_queue_mutex_);
            log_accepting_ = false;
        }
    }
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
        fprintf(stderr, "[DB] request-log pre-BEGIN ROLLBACK error: %s\n",
                sqlite3_errmsg(write_db_));
        return false;
    }

    int rc = sqlite3_exec(write_db_, "BEGIN IMMEDIATE", nullptr, nullptr, nullptr);
    if (rc != SQLITE_OK) {
        fprintf(stderr, "[DB] request-log BEGIN error (%d): %s\n", rc,
                sqlite3_errmsg(write_db_));
        return false;
    }
    for (std::size_t i = 0; i < count; ++i) {
        if (!write_log_record_in_transaction(records[i])) {
            if (sqlite3_get_autocommit(write_db_) == 0 &&
                sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr) !=
                    SQLITE_OK)
                fprintf(stderr, "[DB] request-log ROLLBACK error: %s\n",
                        sqlite3_errmsg(write_db_));
            return false;
        }
    }
    rc = sqlite3_exec(write_db_, "COMMIT", nullptr, nullptr, nullptr);
    if (rc == SQLITE_OK) return true;
    fprintf(stderr, "[DB] request-log COMMIT error (%d): %s\n", rc,
            sqlite3_errmsg(write_db_));
    if (sqlite3_get_autocommit(write_db_) == 0 &&
        sqlite3_exec(write_db_, "ROLLBACK", nullptr, nullptr, nullptr) !=
            SQLITE_OK)
        fprintf(stderr, "[DB] request-log ROLLBACK-after-COMMIT error: %s\n",
                sqlite3_errmsg(write_db_));
    // The transaction may already have committed. Replaying is safe because
    // event_id is unique and checked before inserting attempts.
    return false;
}

bool Database::write_log_record_in_transaction(const LogRecord &record) {
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
        fprintf(stderr, "[DB] request-log event lookup error (%d): %s\n", rc,
                sqlite3_errmsg(write_db_));
        return false;
    }

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
    sqlite3_bind_int(stmt_insert_log_, 22, record.cost_frozen ? 1 : 0);

    rc = sqlite3_step(stmt_insert_log_);
    sqlite3_reset(stmt_insert_log_);
    if (rc != SQLITE_DONE) {
        fprintf(stderr,
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
        sqlite3_bind_int(stmt_insert_attempt_, 3, attempt.account_id);
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
        rc = sqlite3_step(stmt_insert_attempt_);
        sqlite3_reset(stmt_insert_attempt_);
        if (rc != SQLITE_DONE) {
            fprintf(stderr,
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
        fprintf(stderr, "[DB] request-log last_used UPDATE error (%d): %s\n",
                rc, sqlite3_errmsg(write_db_));
        return false;
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
                           double *out_cost) {
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
            fprintf(stderr,
                    "[DB] request-log attempts truncated from %zu to %zu\n",
                    attempts.size(), kLogAttemptsMax);
        }

        double frozen_cost = 0.0;
        record.cost_frozen = snapshot_request_cost(
            record.model, record.prompt_tokens, record.completion_tokens,
            record.cache_read_tokens, record.requested_at_unix, frozen_cost);
        if (record.cost_frozen) {
            record.cost = frozen_cost;
        } else {
            fprintf(stderr,
                    "[DB] WARNING: request cost was not frozen at enqueue; "
                    "legacy trigger pricing will be used (model=%s)\n",
                    record.model.c_str());
        }

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
        fprintf(stderr, "[DB] request-log record allocation error: %s\n",
                e.what());
        return false;
    } catch (...) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr, "[DB] request-log record allocation error: unknown exception\n");
        return false;
    }

    std::string payload;
    try {
        payload = serialize_log_record(record);
    } catch (const std::exception &e) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr, "[DB] request-log serialization error: %s\n",
                e.what());
        return false;
    }
    if (payload.size() > kLogRecordMaxBytes) {
        log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
        fprintf(stderr,
                "[DB] request-log record exceeds spool limit (%zu > %zu)\n",
                payload.size(), kLogRecordMaxBytes);
        return false;
    }

    {
        std::lock_guard<std::mutex> lock(log_queue_mutex_);
        if (!log_accepting_) {
            log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
            fprintf(stderr,
                    "[DB] request-log rejected because shutdown has begun "
                    "(account=%d status=%d)\n",
                    account_id, status_code);
            return false;
        }
        if (!append_log_spool_locked(payload)) {
            log_persist_failures_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
    }
    log_queue_cv_.notify_one();
    // The frozen cost is the same figure the persist path writes (either
    // snapshot_request_cost above or the legacy trigger), so surfacing it here
    // lets the caller's in-memory ledger accumulate without a follow-up read.
    if (out_cost) *out_cost = record.cost;
    return true;
}

// ── resolve_aggregate ────────────────────────────────────────────────────

std::vector<Database::AggregateEntry>
Database::resolve_aggregate(int account_id, const std::string &model) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
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
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
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
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

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

// ── get_timeout_config ──────────────────────────────────────────────────

Database::TimeoutConfig Database::get_timeout_config(const std::string &app_type) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    TimeoutConfig tc;
    sqlite3_reset(stmt_get_timeout_config_);
    sqlite3_bind_text(stmt_get_timeout_config_, 1,
                      app_type.c_str(), app_type.size(), SQLITE_STATIC);

    if (sqlite3_step(stmt_get_timeout_config_) == SQLITE_ROW) {
        tc.streaming_first_byte_timeout = sqlite3_column_int(stmt_get_timeout_config_, 0);
        tc.streaming_idle_timeout = sqlite3_column_int(stmt_get_timeout_config_, 1);
        tc.non_streaming_timeout = sqlite3_column_int(stmt_get_timeout_config_, 2);
    }
    // Missing row (or migration not applied on an ancient DB) → struct defaults.

    sqlite3_reset(stmt_get_timeout_config_);
    return tc;
}
