#pragma once

#include "db.h"
#include "json.hpp"
#include "core/account_types.h"
#include "core/logging.h"

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
#include <iomanip>
#include <limits>
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

using json = nlohmann::json;

// Disk format is frozen. Any framing/checksum change requires an explicit
// data migration and a replacement fixture set; ordinary refactors must not
// change these bytes.
inline constexpr std::size_t kSpoolHeaderBytes = 8;

inline std::uint32_t spool_checksum(const char *data, std::size_t size) {
    std::uint32_t hash = 2166136261u;
    for (std::size_t index = 0; index < size; ++index) {
        hash ^= static_cast<unsigned char>(data[index]);
        hash *= 16777619u;
    }
    return hash;
}

inline void append_u32_le(std::string &output, std::uint32_t value) {
    for (int shift = 0; shift < 32; shift += 8)
        output.push_back(static_cast<char>((value >> shift) & 0xffu));
}

inline std::uint32_t read_u32_le(const char *data) {
    std::uint32_t value = 0;
    for (int index = 0; index < 4; ++index)
        value |= static_cast<std::uint32_t>(
            static_cast<unsigned char>(data[index])) << (index * 8);
    return value;
}

inline bool pread_exact(int fd, void *buffer, std::size_t size,
                        std::uint64_t offset) {
    auto *output = static_cast<char *>(buffer);
    std::size_t done = 0;
    while (done < size) {
        const ssize_t count = ::pread(fd, output + done, size - done,
                                      static_cast<off_t>(offset + done));
        if (count == 0) return false;
        if (count < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        done += static_cast<std::size_t>(count);
    }
    return true;
}

inline bool write_exact(int fd, const char *data, std::size_t size) {
    std::size_t done = 0;
    while (done < size) {
        const ssize_t count = ::write(fd, data + done, size - done);
        if (count < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (count == 0) return false;
        done += static_cast<std::size_t>(count);
    }
    return true;
}

inline std::string bounded_string(const std::string &value,
                                  std::size_t limit) {
    return value.size() <= limit ? value : value.substr(0, limit);
}

class ReadTransactionGuard {
public:
    explicit ReadTransactionGuard(sqlite3 *database) : database_(database) {}
    ~ReadTransactionGuard() {
        if (database_)
            sqlite3_exec(database_, "ROLLBACK", nullptr, nullptr, nullptr);
    }
    void release() noexcept { database_ = nullptr; }
private:
    sqlite3 *database_;
};
