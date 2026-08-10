#pragma once

#include "db.h"

#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace routing {

// Immutable runtime records owned once by a RoutingSnapshot.  Request
// candidates and route handles reference these vectors by index and keep only
// shared_ptr aliases into them — they never copy URLs, paths or secrets.  The
// snapshot outlives every in-flight request because RouteResult retains it.

struct UpstreamRuntime {
    int upstream_id = 0;                      // Database upstreams.id
    std::shared_ptr<const Database::AccountInfo> account_ref;  // single copy
};

struct CredentialRuntime {
    int key_slot_id = 0;                      // upstream_credentials.runtime_id
    std::string uuid;                         // credential UUID (identity across reloads)
    std::shared_ptr<const Database::KeySlot> key_ref;          // single copy
};

struct RouteRuleRuntime {
    std::string model_pattern;                // glob pattern
    int priority_group = 0;
    std::uint32_t upstream_index = 0;         // into upstreams[]
    std::uint32_t cred_start = 0;             // into credential_links[]
    std::uint32_t cred_count = 0;
    std::uint32_t model_index = 0;            // into target_models[]; 0 == preserve client model
};

struct RouteSetRuntime {
    std::string name;
    std::uint32_t rule_start = 0;             // into route_rules[]
    std::uint32_t rule_count = 0;
};

struct RouteEntry {
    std::uint32_t route_set_index = 0;        // into route_sets[]
    int route_set_id = 0;                     // Database route_sets.id (log/health identity)
    int local_key_id = 0;                     // client_keys.id
};

// A resolved rule reference: which upstream + credential to try, which
// interned target model to forward (0 == preserve the client model), and the
// rule's priority group for candidate ordering.  No ownership — the snapshot
// that owns these stays alive for the request.
struct CandidateRef {
    std::uint32_t upstream_index = 0;
    std::uint32_t credential_index = 0;
    std::uint32_t model_index = 0;
    int priority_group = 0;
};

struct RoutingSnapshot {
    std::uint64_t generation = 0;
    std::vector<UpstreamRuntime> upstreams;
    std::vector<CredentialRuntime> credentials;
    std::vector<std::uint32_t> credential_links;   // per-rule credential index span
    std::vector<std::shared_ptr<const std::string>> target_models;  // index 0 == preserve
    std::vector<RouteRuleRuntime> route_rules;
    std::vector<RouteSetRuntime> route_sets;
    // Client key value → route.  std::string hashing is the ClientKeyHash: one
    // hash per request, no scan; collisions resolve exactly because the value
    // is the map key.
    std::unordered_map<std::string, RouteEntry> client_to_route;
    std::unordered_map<std::string, Database::TimeoutConfig> timeouts;
};

}  // namespace routing
