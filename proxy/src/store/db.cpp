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
