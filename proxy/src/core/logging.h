#pragma once

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <string>

enum class LogLevel : int { Error = 0, Warn = 1, Info = 2, Debug = 3 };

inline std::atomic<int> tb_log_level{static_cast<int>(LogLevel::Info)};

inline bool set_log_level(const std::string &name) {
    LogLevel level;
    if (name == "error") level = LogLevel::Error;
    else if (name == "warn") level = LogLevel::Warn;
    else if (name == "info") level = LogLevel::Info;
    else if (name == "debug") level = LogLevel::Debug;
    else return false;
    tb_log_level.store(static_cast<int>(level), std::memory_order_release);
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

inline void log_message(LogLevel level, const char *format, ...) {
    if (!log_enabled(level)) return;
    char stamp[40];
    log_timestamp(stamp, sizeof stamp);
    va_list args;
    va_start(args, format);
    FILE *stream = log_stream(level);
    // Keep the timestamp, level and message one atomic write block so lines
    // from concurrent request threads cannot interleave.
    ::flockfile(stream);
    std::fprintf(stream, "%s %s ", stamp, log_level_name(level));
    std::vfprintf(stream, format, args);
    std::fflush(stream);
    ::funlockfile(stream);
    va_end(args);
}

#define TB_LOG_ERROR(...) log_message(LogLevel::Error, __VA_ARGS__)
#define TB_LOG_WARN(...)  log_message(LogLevel::Warn, __VA_ARGS__)
#define TB_LOG_INFO(...)  log_message(LogLevel::Info, __VA_ARGS__)
#define TB_LOG_DEBUG(...) log_message(LogLevel::Debug, __VA_ARGS__)
