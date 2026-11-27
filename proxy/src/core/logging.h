#pragma once

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <mutex>
#include <string>

enum class LogLevel : int { Error = 0, Warn = 1, Info = 2, Debug = 3 };

inline std::atomic<int> tb_log_level{static_cast<int>(LogLevel::Info)};

/// True once the caller (the --log-level flag or a config file) chose the
/// level explicitly.  TB_PROXY_LOG_LEVEL only seeds the default and never
/// overrides an explicit choice, so a command-line flag always wins.
inline std::atomic<bool> tb_log_level_explicit{false};

inline bool set_log_level(const std::string &name) {
    LogLevel level;
    if (name == "error") level = LogLevel::Error;
    else if (name == "warn") level = LogLevel::Warn;
    else if (name == "info") level = LogLevel::Info;
    else if (name == "debug") level = LogLevel::Debug;
    else return false;
    tb_log_level.store(static_cast<int>(level), std::memory_order_release);
    tb_log_level_explicit.store(true, std::memory_order_release);
    return true;
}

inline bool log_enabled(LogLevel level) {
    return static_cast<int>(level) <=
           tb_log_level.load(std::memory_order_acquire);
}

inline FILE *log_stream(LogLevel level) {
    // Keep warnings and errors visible to service managers while allowing
    // informational/debug output to be discarded by a service's stdout
    // policy without losing it in the foreground debug mode.
    return level == LogLevel::Error || level == LogLevel::Warn ?
        stderr : stdout;
}

inline const char *log_level_name(LogLevel level) noexcept {
    switch (level) {
        case LogLevel::Error: return "ERROR";
        case LogLevel::Warn: return "WARN";
        case LogLevel::Info: return "INFO";
        case LogLevel::Debug: return "DEBUG";
    }
    return "INFO";
}

/// Format the current UTC time as an ISO-8601 millisecond stamp.  Journald
/// adds its own timestamp, but foreground output and redirected files have
/// none, so every emitted line carries this prefix.
inline void log_timestamp(char *out, std::size_t size) noexcept {
    using namespace std::chrono;
    const auto now = system_clock::now();
    const auto millis =
        duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
    const std::time_t seconds = system_clock::to_time_t(now);
    std::tm utc{};
    gmtime_r(&seconds, &utc);
    std::snprintf(out, size, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                  utc.tm_year + 1900, utc.tm_mon + 1, utc.tm_mday,
                  utc.tm_hour, utc.tm_min, utc.tm_sec,
                  static_cast<int>(millis.count()));
}

/// Archive log file (04-C-19).
///
/// Every enabled line keeps going to its stdout/stderr stream (service
/// managers keep their view) and is additionally appended to TB_PROXY_LOG_FILE
/// when that path is configured.  Configuration is environment-only so the
/// sink needs no CLI/config plumbing:
///
///   TB_PROXY_LOG_FILE       path of the live log file; unset/empty = stream only
///   TB_PROXY_LOG_MAX_BYTES  rotate once the live file would exceed this many
///                           bytes (default 8388608; 0 = never rotate)
///   TB_PROXY_LOG_MAX_FILES  files kept in the rotation set, live file included
///                           (default 3; 1 = the live file is restarted in place)
///   TB_PROXY_LOG_LEVEL      seed level (error/warn/info/debug) used only when
///                           no --log-level/config choice was made
///
/// A configured file is opened lazily on the first emitted line, so a process
/// that never logs never creates an empty archive — and an unopenable path is
/// reported on stderr instead of being swallowed.
namespace tb_log_file {

struct Sink {
    std::mutex mutex;
    FILE *file = nullptr;
    std::string path;
    std::uintmax_t max_bytes = 8u * 1024u * 1024u;
    int max_files = 3;
    std::uintmax_t written = 0;
};

inline Sink &sink() {
    // Function-local static: no static-initialisation order dependency for
    // the TUs that log before main().
    static Sink instance;
    return instance;
}

/// Report a sink-level event on stderr directly.  Never re-enters
/// log_message(): the caller may already hold the sink mutex.
inline void notice(const char *format, ...) {
    char stamp[40];
    log_timestamp(stamp, sizeof stamp);
    std::fprintf(stderr, "%s WARN ", stamp);
    va_list args;
    va_start(args, format);
    std::vfprintf(stderr, format, args);
    va_end(args);
    std::fflush(stderr);
}

inline bool open_locked(Sink &state) {
    state.file = std::fopen(state.path.c_str(), "a");
    state.written = 0;
    if (state.file == nullptr) return false;
    if (std::fseek(state.file, 0, SEEK_END) == 0) {
        const long size = std::ftell(state.file);
        if (size > 0) state.written = static_cast<std::uintmax_t>(size);
    }
    std::clearerr(state.file);
    return true;
}

inline void rotate_locked(Sink &state) {
    if (state.file != nullptr) {
        std::fflush(state.file);
        std::fclose(state.file);
        state.file = nullptr;
    }
    if (state.max_files <= 1) {
        // One-file rotation: restart the live file in place.
        std::remove(state.path.c_str());
    } else {
        const std::string oldest =
            state.path + "." + std::to_string(state.max_files - 1);
        std::remove(oldest.c_str());
        for (int index = state.max_files - 2; index >= 1; --index) {
            const std::string from = state.path + "." + std::to_string(index);
            const std::string to =
                state.path + "." + std::to_string(index + 1);
            std::rename(from.c_str(), to.c_str());
        }
        std::rename(state.path.c_str(), (state.path + ".1").c_str());
    }
    if (!open_locked(state))
        notice("proxy log file reopen failed after rotation: %s\n",
               state.path.c_str());
    else
        notice("proxy log rotated: %s (%ju bytes, keeping %d file(s))\n",
               state.path.c_str(),
               static_cast<std::uintmax_t>(state.max_bytes), state.max_files);
}

inline bool configure(const std::string &path, std::uintmax_t max_bytes,
                      int max_files) {
    Sink &state = sink();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (state.file != nullptr) {
        std::fflush(state.file);
        std::fclose(state.file);
        state.file = nullptr;
    }
    state.path = path;
    state.max_bytes = max_bytes;
    state.max_files = max_files < 1 ? 1 : max_files;
    state.written = 0;
    if (path.empty()) return true;
    if (!open_locked(state)) {
        notice("proxy log file open failed: %s\n", path.c_str());
        return false;
    }
    notice("proxy log file: %s (level %s, rotate at %ju bytes, keep %d)\n",
           path.c_str(),
           log_level_name(static_cast<LogLevel>(
               tb_log_level.load(std::memory_order_acquire))),
           static_cast<std::uintmax_t>(max_bytes), state.max_files);
    return true;
}

inline void append(const std::string &text) {
    Sink &state = sink();
    std::lock_guard<std::mutex> lock(state.mutex);
    if (state.file == nullptr) return;
    // A single line longer than the whole budget is written as-is instead of
    // rotating on every line (rotation only triggers once the file has
    // content).
    if (state.max_bytes > 0 && state.written > 0 &&
        state.written + text.size() + 1 > state.max_bytes)
        rotate_locked(state);
    if (state.file == nullptr) return;
    state.written += std::fwrite(text.data(), 1, text.size(), state.file);
    std::fflush(state.file);
}

}  // namespace tb_log_file

inline std::uintmax_t log_env_bytes(const char *name,
                                    std::uintmax_t fallback) {
    const char *value = std::getenv(name);
    if (value == nullptr || *value == '\0') return fallback;
    char *end = nullptr;
    const unsigned long long parsed = std::strtoull(value, &end, 10);
    if (end == value || (end != nullptr && *end != '\0')) return fallback;
    return static_cast<std::uintmax_t>(parsed);
}

inline int log_env_count(const char *name, int fallback) {
    const std::uintmax_t parsed =
        log_env_bytes(name, static_cast<std::uintmax_t>(fallback));
    if (parsed == 0) return fallback;
    return parsed > 1000u ? 1000 : static_cast<int>(parsed);
}

/// Apply the TB_PROXY_LOG_* environment once, on the first emitted line.
/// Idempotent and safe under concurrent logging.
inline void initialize_logging() {
    static std::once_flag once;
    std::call_once(once, [] {
        if (!tb_log_level_explicit.load(std::memory_order_acquire)) {
            const char *level = std::getenv("TB_PROXY_LOG_LEVEL");
            if (level != nullptr && *level != '\0' && !set_log_level(level))
                tb_log_file::notice(
                    "TB_PROXY_LOG_LEVEL=%s is not one of "
                    "error|warn|info|debug; keeping %s\n",
                    level,
                    log_level_name(static_cast<LogLevel>(
                        tb_log_level.load(std::memory_order_acquire))));
        }
        const char *path = std::getenv("TB_PROXY_LOG_FILE");
        if (path == nullptr || *path == '\0') return;
        tb_log_file::configure(
            path, log_env_bytes("TB_PROXY_LOG_MAX_BYTES", 8u * 1024u * 1024u),
            log_env_count("TB_PROXY_LOG_MAX_FILES", 3));
    });
}

inline void log_message(LogLevel level, const char *format, ...) {
    if (!log_enabled(level)) return;
    initialize_logging();
    char stamp[40];
    log_timestamp(stamp, sizeof stamp);
    va_list args;
    va_start(args, format);
    va_list measure;
    va_copy(measure, args);
    const int needed = std::vsnprintf(nullptr, 0, format, measure);
    va_end(measure);
    std::string message;
    if (needed > 0) {
        std::string buffer(static_cast<std::size_t>(needed) + 1, '\0');
        std::vsnprintf(buffer.data(), buffer.size(), format, args);
        buffer.resize(static_cast<std::size_t>(needed));
        message = std::move(buffer);
    }
    va_end(args);
    // Timestamp, level and message form one write block so lines from
    // concurrent request threads cannot interleave in either destination.
    std::string line(stamp);
    line.push_back(' ');
    line.append(log_level_name(level));
    line.push_back(' ');
    line.append(message);
    tb_log_file::append(line);
    FILE *stream = log_stream(level);
    ::flockfile(stream);
    std::fwrite(line.data(), 1, line.size(), stream);
    std::fflush(stream);
    ::funlockfile(stream);
}

#define TB_LOG_ERROR(...) log_message(LogLevel::Error, __VA_ARGS__)
#define TB_LOG_WARN(...)  log_message(LogLevel::Warn, __VA_ARGS__)
#define TB_LOG_INFO(...)  log_message(LogLevel::Info, __VA_ARGS__)
#define TB_LOG_DEBUG(...) log_message(LogLevel::Debug, __VA_ARGS__)
