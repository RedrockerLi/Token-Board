#include "router.h"
#include "db.h"

Router::RouteResult Router::route(const std::string &local_key) {
    // Check cache first
    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        auto it = cache_.find(local_key);
        if (it != cache_.end()) {
            if (std::chrono::steady_clock::now() < it->second.expires_at) {
                return it->second.result;
            }
            // Expired — remove and fall through
            cache_.erase(it);
        }
    }

    // Look up in database
    auto key_info = db_.lookup_local_key(local_key);

    RouteResult result;
    if (!key_info.has_value()) {
        result.success = false;
        result.error = "Invalid API key";
        // Cache failures briefly to avoid DB spam
        std::lock_guard<std::mutex> lock(cache_mutex_);
        CacheEntry entry;
        entry.result = result;
        entry.expires_at = std::chrono::steady_clock::now() +
                           std::chrono::seconds(10);  // shorter TTL for misses
        cache_[local_key] = entry;
        return result;
    }

    auto account = db_.get_account(key_info->account_id);
    if (!account.has_value()) {
        result.success = false;
        result.error = "Account not found or inactive";
        std::lock_guard<std::mutex> lock(cache_mutex_);
        CacheEntry entry;
        entry.result = result;
        entry.expires_at = std::chrono::steady_clock::now() +
                           std::chrono::seconds(10);
        cache_[local_key] = entry;
        return result;
    }

    result.success = true;
    result.upstream_key = account->upstream_key;
    result.base_url = account->base_url;
    result.api_format = account->api_format;
    result.endpoint_path = account->endpoint_path;
    result.auth_header = account->auth_header;
    result.account_id = account->id;
    result.local_key_id = key_info->id;
    result.is_aggregate = account->is_aggregate;

    // Cache the successful result
    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        CacheEntry entry;
        entry.result = result;
        entry.expires_at = std::chrono::steady_clock::now() +
                           std::chrono::seconds(CACHE_TTL_SEC);
        cache_[local_key] = entry;
    }

    return result;
}
