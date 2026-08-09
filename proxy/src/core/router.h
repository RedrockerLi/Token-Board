#pragma once

#include "db.h"

#include <chrono>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

struct RoutingSnapshot;

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
    explicit Router(Database &db);
    ~Router();
    Router(const Router &) = delete;
    Router &operator=(const Router &) = delete;

    struct RouteResult {
        bool success = false;
        std::string error;       // human-readable when !success
        std::string base_url;
        std::string api_format;      // "openai" | "openai_responses" | "anthropic"
        std::string endpoint_path;   // "" = derive from api_format
        std::string auth_header;     // "bearer" | "x-api-key"
        int account_id = 0;
        int local_key_id = 0;
        Database::AccountInfo account;
        std::shared_ptr<const RoutingSnapshot> snapshot;
    };

    RouteResult route(const std::string &local_key);
    std::vector<Database::RoutingTarget> resolve_targets(
        const RouteResult &route, const std::string &model) const;
    std::vector<std::string> model_patterns(const RouteResult &route) const;
    void shutdown();

private:
    bool reload();
    void refresh_loop();
    Database &db_;
    std::shared_ptr<const RoutingSnapshot> snapshot_;
    std::thread refresh_thread_;
    std::atomic<bool> stop_{false};
};
