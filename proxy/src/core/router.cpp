#include "router.h"
#include "candidate_selection.h"
#include "db.h"
#include "upstream_client.h"
#include "logging.h"
#include "json.hpp"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <unordered_map>
#include <unordered_set>

namespace {

// "YYYY-MM-DDThh:mm:ss[.fff]Z" → epoch seconds; 0 when unparseable.  The
// usage poller writes UTC timestamps only, so a missing designator is read as
// UTC rather than as local time.
std::int64_t parse_utc_timestamp(const std::string &value) {
    std::tm tm{};
    int year = 0, month = 0, day = 0, hour = 0, minute = 0, second = 0;
    if (std::sscanf(value.c_str(), "%4d-%2d-%2dT%2d:%2d:%2d", &year, &month,
                    &day, &hour, &minute, &second) != 6)
        return 0;
    if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 ||
        minute > 59 || second > 60 || year < 1970)
        return 0;
    tm.tm_year = year - 1900;
    tm.tm_mon = month - 1;
    tm.tm_mday = day;
    tm.tm_hour = hour;
    tm.tm_min = minute;
    tm.tm_sec = second < 60 ? second : 59;
    tm.tm_isdst = 0;
    const std::time_t epoch = timegm(&tm);
    return epoch > 0 ? static_cast<std::int64_t>(epoch) : 0;
}

}  // namespace

// Shared with the cooldown-recovery path (declared in router.h): an
// opencode-go cooldown clears as soon as this parse reports quota again.

// Parse one credential's usage_json into the frozen snapshot shape:
//   {"checked_at": …, "error": null|"…", "windows": {"5h": {...}, "week": …,
//    "month": …}, "monthly_period": {...}}
// Unusable input means "no usage data" (a null result), so the credential then
// has no deadline and is never treated as exhausted.  Fields are read one at a
// time: a single window with a bad payload must not discard the others.
std::shared_ptr<const routing::CredentialUsage> parse_credential_usage(
    int key_slot_id, const std::string &text) {
    if (text.empty()) return nullptr;
    routing::CredentialUsage usage;
    try {
        const auto root = nlohmann::json::parse(text);
        if (!root.is_object()) return nullptr;
        const auto windows = root.find("windows");
        if (windows == root.end() || !windows->is_object()) return nullptr;
        for (const char *name : {"5h", "week", "month"}) {
            const auto window = windows->find(name);
            if (window == windows->end() || !window->is_object()) continue;
            const auto percent = window->find("percent");
            if (percent == window->end() || !percent->is_number()) continue;
            routing::UsageWindow entry;
            entry.name = name;
            entry.percent = static_cast<int>(
                std::clamp(percent->get<double>(), 0.0, 100.0));
            const auto reset = window->find("resets_at");
            if (reset != window->end() && reset->is_string())
                entry.resets_at = parse_utc_timestamp(reset->get<std::string>());
            if (entry.percent >= kExhaustedPercent) usage.exhausted = true;
            usage.windows.push_back(std::move(entry));
        }
    } catch (const std::exception &e) {
        TB_LOG_WARN("[Router] credential %d has an unreadable usage snapshot "
                    "(%s); scheduling it without usage data\n",
                    key_slot_id, e.what());
        return nullptr;
    } catch (...) {
        TB_LOG_WARN("[Router] credential %d has an unreadable usage snapshot; "
                    "scheduling it without usage data\n", key_slot_id);
        return nullptr;
    }
    if (usage.windows.empty()) return nullptr;
    return std::make_shared<const routing::CredentialUsage>(std::move(usage));
}

namespace {

bool glob_match(const std::string &pattern, const std::string &value) {
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

std::string origin_from_url(const std::string &url) {
    const auto scheme_end = url.find("://");
    if (scheme_end == std::string::npos) return url;
    const auto path = url.find('/', scheme_end + 3);
    return path == std::string::npos ? url : url.substr(0, path);
}

std::unordered_set<std::string> snapshot_origins(
    const std::shared_ptr<const routing::RoutingSnapshot> &snapshot) {
    std::unordered_set<std::string> result;
    if (!snapshot) return result;
    for (const auto &upstream : snapshot->upstreams)
        if (upstream.account_ref && !upstream.account_ref->base_url.empty())
            result.insert(origin_from_url(upstream.account_ref->base_url));
    return result;
}

}  // namespace

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
    const auto previous = std::atomic_load_explicit(
        &snapshot_, std::memory_order_acquire);
    auto next = std::make_shared<routing::RoutingSnapshot>();
    next->generation = config.generation;

    // Interned target-model strings.  Index 0 is the "preserve client model"
    // sentinel; rules that forward a fixed model reference a non-null entry.
    next->target_models.push_back(nullptr);

    // Deduplicate upstreams and credentials into continuous vectors so each
    // URL/path/secret lives exactly once in the snapshot; rules reference by
    // index.
    std::unordered_map<int, std::uint32_t> upstream_by_id;
    std::unordered_map<int, std::uint32_t> credential_by_id;
    std::vector<std::uint32_t> rule_route_sets;   // parallel to route_rules
    for (const auto &rule : config.rules) {
        const auto &account_ref = rule.target.account_ref;
        if (!account_ref) continue;
        auto up_it = upstream_by_id.find(account_ref->upstream_id);
        std::uint32_t upstream_index = 0;
        if (up_it != upstream_by_id.end()) {
            upstream_index = up_it->second;
        } else {
            routing::UpstreamRuntime up;
            up.upstream_id = account_ref->upstream_id;
            up.account_ref = account_ref;
            upstream_index = static_cast<std::uint32_t>(next->upstreams.size());
            upstream_by_id.emplace(account_ref->upstream_id, upstream_index);
            next->upstreams.push_back(std::move(up));
        }

        const std::uint32_t cred_start =
            static_cast<std::uint32_t>(next->credential_links.size());
        for (const auto &key_ref : rule.target.key_refs) {
            if (!key_ref || key_ref->key_value.empty()) continue;
            auto key_it = credential_by_id.find(key_ref->id);
            std::uint32_t cred_index = 0;
            if (key_it != credential_by_id.end()) {
                cred_index = key_it->second;
            } else {
                routing::CredentialRuntime cred;
                cred.key_slot_id = key_ref->id;
                cred.key_ref = key_ref;
                cred.usage_ref =
                    parse_credential_usage(key_ref->id, key_ref->usage_json);
                cred_index = static_cast<std::uint32_t>(next->credentials.size());
                credential_by_id.emplace(key_ref->id, cred_index);
                next->credentials.push_back(std::move(cred));
            }
            next->credential_links.push_back(cred_index);
        }

        std::uint32_t model_index = 0;
        if (rule.target.upstream_model_ref) {
            auto found = std::find_if(
                next->target_models.begin(), next->target_models.end(),
                [&](const std::shared_ptr<const std::string> &entry) {
                    return entry && *entry == *rule.target.upstream_model_ref;
                });
            if (found != next->target_models.end()) {
                model_index = static_cast<std::uint32_t>(
                    std::distance(next->target_models.begin(), found));
            } else {
                model_index =
                    static_cast<std::uint32_t>(next->target_models.size());
                next->target_models.push_back(rule.target.upstream_model_ref);
            }
        }

        routing::RouteRuleRuntime rr;
        rr.model_pattern = rule.model_pattern;
        rr.priority_group = rule.target.priority_group;
        rr.upstream_index = upstream_index;
        rr.cred_start = cred_start;
        rr.cred_count = static_cast<std::uint32_t>(
            next->credential_links.size() - cred_start);
        rr.model_index = model_index;
        rule_route_sets.push_back(static_cast<std::uint32_t>(rule.route_set_id));
        next->route_rules.push_back(std::move(rr));
    }

    // Group rules into route sets.  load_routing_config orders rules by
    // route_set_id, so each set occupies a contiguous span.
    std::unordered_map<int, std::uint32_t> route_set_index_by_id;
    for (std::size_t r = 0; r < next->route_rules.size();) {
        const int route_set_id = static_cast<int>(rule_route_sets[r]);
        routing::RouteSetRuntime set;
        set.rule_start = static_cast<std::uint32_t>(r);
        std::size_t end = r + 1;
        while (end < next->route_rules.size() &&
               rule_route_sets[end] == rule_route_sets[r])
            ++end;
        set.rule_count = static_cast<std::uint32_t>(end - r);
        route_set_index_by_id[route_set_id] =
            static_cast<std::uint32_t>(next->route_sets.size());
        next->route_sets.push_back(std::move(set));
        r = end;
    }
    // A client key may reference a route set with no rules (e.g. an aggregate
    // catalog with no ordinary rule): keep an empty set so the key resolves to
    // an empty candidate list instead of "Invalid API key".
    for (const auto &route : config.routes) {
        const int route_set_id = route.key.account_id;
        if (route_set_index_by_id.count(route_set_id)) continue;
        routing::RouteSetRuntime set;
        set.rule_start = static_cast<std::uint32_t>(next->route_rules.size());
        set.rule_count = 0;
        route_set_index_by_id[route_set_id] =
            static_cast<std::uint32_t>(next->route_sets.size());
        next->route_sets.push_back(std::move(set));
    }

    for (const auto &route : config.routes) {
        auto set_it = route_set_index_by_id.find(route.key.account_id);
        if (set_it == route_set_index_by_id.end()) continue;
        routing::RouteEntry entry;
        entry.route_set_index = set_it->second;
        entry.route_set_id = route.account.id;
        entry.local_key_id = route.key.id;
        next->client_to_route.emplace(route.key.key_value, entry);
    }
    next->timeouts = std::move(config.timeouts);

    // Dashboard-maintained model catalogs, attached per upstream: /v1/models
    // serves these verbatim for plain accounts, so no catalog request ever
    // reaches an upstream.  Absent entry = never refreshed ("更新模型").
    for (auto &up : next->upstreams) {
        auto catalog_it = config.catalogs.find(up.upstream_id);
        if (catalog_it == config.catalogs.end() || catalog_it->second.empty())
            continue;
        up.catalog_ref = std::make_shared<const std::vector<std::string>>(
            std::move(catalog_it->second));
    }

    // Only retire origins whose endpoint was removed or changed.  Unchanged
    // keep-alive connections remain hot across ordinary route/key edits.
    const auto old_origins = snapshot_origins(previous);
    const auto new_origins = snapshot_origins(next);
    std::unordered_set<std::string> changed_origins;
    for (const auto &origin : old_origins)
        if (!new_origins.count(origin)) changed_origins.insert(origin);
    UpstreamClient::invalidate_connections(changed_origins);
    std::atomic_store_explicit(&snapshot_,
        std::static_pointer_cast<const routing::RoutingSnapshot>(next),
        std::memory_order_release);
    return true;
}

Database::TimeoutConfig Router::timeout_config(
    const std::string &endpoint) const {
    auto snapshot = std::atomic_load_explicit(&snapshot_,
                                              std::memory_order_acquire);
    if (!snapshot) return {};
    auto found = snapshot->timeouts.find(endpoint);
    return found == snapshot->timeouts.end() ? Database::TimeoutConfig{}
                                              : found->second;
}

bool Router::snapshot_loaded() const noexcept {
    return static_cast<bool>(std::atomic_load_explicit(
        &snapshot_, std::memory_order_acquire));
}

std::uint64_t Router::snapshot_generation() const noexcept {
    const auto snapshot = std::atomic_load_explicit(
        &snapshot_, std::memory_order_acquire);
    return snapshot ? snapshot->generation : 0;
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
                TB_LOG_ERROR("[Router] routing snapshot rebuild failed; "
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
    auto found = snapshot->client_to_route.find(local_key);
    if (found == snapshot->client_to_route.end()) return {false, "Invalid API key"};
    RouteResult result;
    result.success = true;
    result.account_id = found->second.route_set_id;
    result.local_key_id = found->second.local_key_id;
    result.route_set_index = found->second.route_set_index;
    result.snapshot = std::move(snapshot);
    return result;
}

std::vector<routing::CandidateRef> Router::resolve_targets(
    const RouteResult &route, const std::string &model) const {
    auto snapshot = route.snapshot ? route.snapshot
        : std::atomic_load_explicit(&snapshot_, std::memory_order_acquire);
    std::vector<routing::CandidateRef> refs;
    if (!snapshot || route.route_set_index >= snapshot->route_sets.size())
        return refs;
    const auto &set = snapshot->route_sets[route.route_set_index];
    const auto span_end = set.rule_start + set.rule_count;
    for (std::uint32_t i = set.rule_start; i < span_end; ++i) {
        const auto &rule = snapshot->route_rules[i];
        if (!glob_match(rule.model_pattern, model)) continue;
        for (std::uint32_t c = 0; c < rule.cred_count; ++c) {
            routing::CandidateRef ref;
            ref.upstream_index = rule.upstream_index;
            ref.credential_index =
                snapshot->credential_links[rule.cred_start + c];
            ref.model_index = rule.model_index;
            ref.priority_group = rule.priority_group;
            refs.push_back(ref);
        }
    }
    return refs;
}

std::vector<std::string> Router::model_patterns(const RouteResult &route) const {
    auto snapshot = route.snapshot ? route.snapshot
        : std::atomic_load_explicit(&snapshot_, std::memory_order_acquire);
    std::vector<std::string> result;
    if (!snapshot || route.route_set_index >= snapshot->route_sets.size())
        return result;
    const auto &set = snapshot->route_sets[route.route_set_index];
    std::unordered_set<std::string> seen;
    const auto span_end = set.rule_start + set.rule_count;
    for (std::uint32_t i = set.rule_start; i < span_end; ++i) {
        const auto &rule = snapshot->route_rules[i];
        if (rule.model_pattern != "*" && seen.insert(rule.model_pattern).second)
            result.push_back(rule.model_pattern);
    }
    return result;
}
