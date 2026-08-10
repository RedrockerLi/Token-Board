#pragma once

#include "db.h"
#include "routing_snapshot.h"

#include <chrono>
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

/// Resolves client keys and model rules from an immutable in-memory snapshot.
/// SQLite is read only by the background generation watcher; request threads
/// retain the snapshot they started with so a concurrent refresh cannot
/// invalidate candidate references.
class Router {
public:
    explicit Router(Database &db);
    ~Router();
    Router(const Router &) = delete;
    Router &operator=(const Router &) = delete;

    struct RouteResult {
        bool success = false;
        std::string error;       // human-readable when !success
        int account_id = 0;      // route-set id (kept for logging)
        int local_key_id = 0;    // client_keys.id
        std::uint32_t route_set_index = 0;  // into RoutingSnapshot::route_sets
        std::shared_ptr<const routing::RoutingSnapshot> snapshot;
    };

    RouteResult route(const std::string &local_key);
    std::vector<routing::CandidateRef> resolve_targets(
        const RouteResult &route, const std::string &model) const;
    std::vector<std::string> model_patterns(const RouteResult &route) const;
    Database::TimeoutConfig timeout_config(const std::string &endpoint) const;
    bool snapshot_loaded() const noexcept;
    std::uint64_t snapshot_generation() const noexcept;
    void shutdown();

private:
    bool reload();
    void refresh_loop();
    Database &db_;
    std::shared_ptr<const routing::RoutingSnapshot> snapshot_;
    std::thread refresh_thread_;
    std::atomic<bool> stop_{false};
};
