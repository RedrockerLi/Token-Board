#include "router.h"
#include "db.h"

Router::RouteResult Router::route(const std::string &local_key) {
    // Short-TTL success cache (C2-2): a hit returns the cached result without
    // touching the DB's read connection, eliminating the per-request
    // read_mutex_-serialized lookup.  TTL is 2s, so revocation/configuration
    // changes propagate almost immediately.  Failures are never cached.
    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        auto it = cache_.find(local_key);
        if (it != cache_.end()) {
            if (std::chrono::steady_clock::now() < it->second.expires_at)
                return it->second.result;
            cache_.erase(it);  // expired — refresh from DB
        }
    }

    auto route_info = db_.lookup_route(local_key);

    RouteResult result;
    if (!route_info.has_value()) {
        result.success = false;
        result.error = "Invalid API key";
        return result;
    }

    const auto &account = route_info->account;
    if (account.deleted) {
        result.success = false;
        result.error = "Account not found or inactive";
        return result;
    }

    result.success = true;
    result.base_url = account.base_url;
    result.api_format = account.api_format;
    result.endpoint_path = account.endpoint_path;
    result.auth_header = account.auth_header;
    result.account_id = account.id;
    result.local_key_id = route_info->key.id;
    result.is_aggregate = account.is_aggregate;
    result.account = account;

    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        cache_[local_key] = {result,
            std::chrono::steady_clock::now() + std::chrono::seconds(kCacheTtlSec)};
    }

    return result;
}
