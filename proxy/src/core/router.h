#pragma once

#include "db.h"

#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>

/// Maps local (proxy) API keys to upstream CSTCloud accounts.
///
/// Authentication is verified against SQLite, but a very short-TTL cache
/// absorbs the per-request `read_mutex_`-serialized lookup: a 2-second window
/// of stale auth/routing is the deliberate price for removing a synchronous
/// SQLite read from every request's hot path.  Only SUCCESSFUL results are
/// cached (a revoked/invalid key stays uncached and is re-checked every
/// request), so a random-key attack cannot grow the cache and key revocation
/// takes effect on the next uncached request.
class Router {
public:
    explicit Router(Database &db) : db_(db) {}

    struct RouteResult {
        bool success = false;
        std::string error;       // human-readable when !success
        std::string upstream_key;
        std::string base_url;
        std::string api_format;      // "openai" | "openai_responses" | "anthropic"
        std::string endpoint_path;   // "" = derive from api_format
        std::string auth_header;     // "bearer" | "x-api-key"
        int account_id = 0;
        int local_key_id = 0;
        bool is_aggregate = false;   // aggregate account — resolve per model
        Database::AccountInfo account;
    };

    RouteResult route(const std::string &local_key);

private:
    Database &db_;

    // Short-TTL success cache (C2-2).  Bounded in practice: only valid keys
    // are cached, so the map is at most the number of distinct active keys.
    static constexpr int kCacheTtlSec = 2;
    struct CacheEntry {
        RouteResult result;
        std::chrono::steady_clock::time_point expires_at;
    };
    std::mutex cache_mutex_;
    std::unordered_map<std::string, CacheEntry> cache_;
};
