#include "router.h"
#include "db.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <unordered_set>

struct RoutingSnapshot {
    std::uint64_t generation = 0;
    std::unordered_map<std::string, Router::RouteResult> routes;
    std::unordered_map<int, std::vector<Database::RoutingRule>> rules;
};

static bool glob_match(const std::string &pattern, const std::string &value) {
    size_t p = 0, v = 0, star = std::string::npos, retry = 0;
    while (v < value.size()) {
        if (p < pattern.size() && (pattern[p] == '?' || pattern[p] == value[v])) {
            ++p; ++v;
        } else if (p < pattern.size() && pattern[p] == '*') {
            star = p++;
            retry = v;
        } else if (star != std::string::npos) {
            p = star + 1;
            v = ++retry;
        } else {
            return false;
        }
    }
    while (p < pattern.size() && pattern[p] == '*') ++p;
    return p == pattern.size();
}

Router::Router(Database &db) : db_(db) {
    reload();
    refresh_thread_ = std::thread(&Router::refresh_loop, this);
}

Router::~Router() { shutdown(); }

void Router::shutdown() {
    stop_.store(true, std::memory_order_release);
    if (refresh_thread_.joinable()) refresh_thread_.join();
}

bool Router::reload() {
    Database::RoutingConfig config;
    if (!db_.load_routing_config(config)) return false;
    auto next = std::make_shared<RoutingSnapshot>();
    next->generation = config.generation;
    for (const auto &route : config.routes) {
        RouteResult result;
        result.success = true;
        result.base_url = route.account.base_url;
        result.api_format = route.account.api_format;
        result.endpoint_path = route.account.endpoint_path;
        result.auth_header = route.account.auth_header;
        result.account_id = route.account.id;
        result.local_key_id = route.key.id;
        result.account = route.account;
        next->routes.emplace(route.key.key_value, std::move(result));
    }
    for (auto &rule : config.rules)
        next->rules[rule.route_set_id].push_back(std::move(rule));
    std::atomic_store_explicit(&snapshot_,
        std::static_pointer_cast<const RoutingSnapshot>(next),
        std::memory_order_release);
    return true;
}

void Router::refresh_loop() {
    while (!stop_.load(std::memory_order_acquire)) {
        for (int i = 0; i < 25 && !stop_.load(std::memory_order_acquire); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        if (stop_.load(std::memory_order_acquire)) break;
        auto current = std::atomic_load_explicit(&snapshot_,
                                                 std::memory_order_acquire);
        const auto generation = db_.routing_config_generation();
        if (!current || generation != current->generation) {
            if (!reload())
                fprintf(stderr, "[Router] routing snapshot rebuild failed; "
                        "continuing with generation %llu\n",
                        static_cast<unsigned long long>(
                            current ? current->generation : 0));
        }
    }
}

Router::RouteResult Router::route(const std::string &local_key) {
    auto snapshot = std::atomic_load_explicit(&snapshot_,
                                              std::memory_order_acquire);
    if (!snapshot) return {false, "Routing configuration unavailable"};
    auto found = snapshot->routes.find(local_key);
    if (found == snapshot->routes.end()) return {false, "Invalid API key"};
    RouteResult result = found->second;
    result.snapshot = std::move(snapshot);
    return result;
}

std::vector<Database::RoutingTarget> Router::resolve_targets(
    const RouteResult &route, const std::string &model) const {
    auto snapshot = route.snapshot ? route.snapshot
        : std::atomic_load_explicit(&snapshot_, std::memory_order_acquire);
    std::vector<Database::RoutingTarget> targets;
    if (!snapshot) return targets;
    auto rules = snapshot->rules.find(route.account_id);
    if (rules == snapshot->rules.end()) return targets;
    for (const auto &rule : rules->second) {
        if (!glob_match(rule.model_pattern, model)) continue;
        auto target = rule.target;
        if (target.upstream_model.empty()) target.upstream_model = model;
        targets.push_back(std::move(target));
    }
    return targets;
}

std::vector<std::string> Router::model_patterns(const RouteResult &route) const {
    auto snapshot = route.snapshot ? route.snapshot
        : std::atomic_load_explicit(&snapshot_, std::memory_order_acquire);
    std::vector<std::string> result;
    if (!snapshot) return result;
    auto rules = snapshot->rules.find(route.account_id);
    if (rules == snapshot->rules.end()) return result;
    std::unordered_set<std::string> seen;
    for (const auto &rule : rules->second)
        if (rule.model_pattern != "*" && seen.insert(rule.model_pattern).second)
            result.push_back(rule.model_pattern);
    return result;
}
