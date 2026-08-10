#include "database_internal.h"

bool Database::run_migrations(const std::string &schema_dir) {
    namespace fs = std::filesystem;
    // Advisory lock — pairs with the Python runner's fcntl.flock().
    int lock_fd = ::open((db_path_ + ".migrate.lock").c_str(), O_CREAT | O_RDWR, 0644);
    if (lock_fd < 0) {
        TB_LOG_ERROR( "[DB] cannot open migration lock\n");
        return false;
    }
    if (flock(lock_fd, LOCK_EX) != 0) {  // blocking
        TB_LOG_ERROR( "[DB] cannot lock migration file: %s\n",
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
                TB_LOG_ERROR( "[DB] schema_version disagrees with "
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
            TB_LOG_ERROR( "[DB] non-empty database has no schema version\n");
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
    }
    const int current_major = encoded_version / 10000;
    const int current_minor = encoded_version % 10000;

    fs::path supplied(schema_dir);
    if (!fs::is_directory(supplied)) {
        TB_LOG_ERROR( "[DB] schema dir not found: %s\n", schema_dir.c_str());
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
            TB_LOG_ERROR( "[DB] no schema/proxy/vN directory below %s\n",
                    schema_dir.c_str());
            flock(lock_fd, LOCK_UN);
            ::close(lock_fd);
            return false;
        }
        selected = majors.back().second;
        if (!empty_database && current_major != majors.back().first) {
                TB_LOG_ERROR(
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
            TB_LOG_ERROR( "[DB] bad migration filename: %s "
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
        TB_LOG_ERROR( "[DB] no migration files in %s\n", selected.c_str());
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return false;
    }
    for (std::size_t i = 0; i < steps.size(); ++i) {
        if (steps[i].major != steps.front().major ||
            (i && steps[i - 1].major == steps[i].major &&
             steps[i - 1].minor == steps[i].minor)) {
            TB_LOG_ERROR( "[DB] mixed/duplicate migration version in %s\n",
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
                    TB_LOG_ERROR( "[DB] checksum mismatch for V%d.%d\n",
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
        TB_LOG_ERROR( "[DB] major schema mismatch: V%d.%d vs V%d\n",
                current_major, current_minor, steps.front().major);
        flock(lock_fd, LOCK_UN);
        ::close(lock_fd);
        return false;
    }
    if (empty_database && steps.front().major >= 1)
        sqlite3_exec(write_db_, "PRAGMA auto_vacuum=INCREMENTAL", nullptr,
                     nullptr, nullptr);
    if (!empty_database && current_minor > steps.back().minor) {
        TB_LOG_ERROR( "[DB] warning: proxy V%d.%d is newer than known V%d.%d; "
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
            TB_LOG_ERROR( "[DB] Migration %s is empty/unreadable\n",
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
            TB_LOG_ERROR( "[DB] Migration %s failed: %s\n",
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
