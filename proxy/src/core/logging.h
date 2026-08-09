#pragma once

#include <atomic>
#include <cstdarg>
#include <cstdio>
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

inline void log_message(LogLevel level, const char *format, ...) {
    if (!log_enabled(level)) return;
    va_list args;
    va_start(args, format);
    std::vfprintf(stderr, format, args);
    va_end(args);
}

#define TB_LOG_ERROR(...) log_message(LogLevel::Error, __VA_ARGS__)
#define TB_LOG_WARN(...)  log_message(LogLevel::Warn, __VA_ARGS__)
#define TB_LOG_INFO(...)  log_message(LogLevel::Info, __VA_ARGS__)
#define TB_LOG_DEBUG(...) log_message(LogLevel::Debug, __VA_ARGS__)
