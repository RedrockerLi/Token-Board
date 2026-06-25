#pragma once

#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>

class Database;

/// Maps local (proxy) API keys to upstream CSTCloud accounts.
///
/// Queries the `local_keys` + `upstream_accounts` tables in SQLite for each
/// lookup and caches results in memory for 60 seconds to reduce DB pressure
/// under load.  Unknown / inactive keys produce an error result.
class Router {
public:
    explicit Router(Database &db) : db_(db) {}

    struct RouteResult {
        bool success = false;
        std::string error;       // human-readable when !success
        std::string upstream_key;
        std::string base_url;
        std::string api_format;  // "openai" or "anthropic"
        int account_id = 0;
        int local_key_id = 0;
    };

    RouteResult route(const std::string &local_key);

private:
    Database &db_;

    struct CacheEntry {
        RouteResult result;
        std::chrono::steady_clock::time_point expires_at;
    };

    std::mutex cache_mutex_;
    std::unordered_map<std::string, CacheEntry> cache_;
    static constexpr int CACHE_TTL_SEC = 60;
};
