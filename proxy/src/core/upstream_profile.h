#pragma once

#include <string>
#include <utility>
#include <vector>

#include "logging.h"

/// How a plan key in cooldown witnesses quota recovery.
///
/// `kHttpProbe` sends one minimal chat call per probe interval; a 2xx means the
/// key can serve again.  `kUsageSnapshot` reads the usage snapshot the
/// dashboard poller refreshes — the gateway's catalog endpoint answers 200 for
/// any or no key, so an HTTP probe cannot witness recovery for it.
enum class CooldownRecoveryChannel { kHttpProbe, kUsageSnapshot };

/// Headers a profile wants added to an upstream request.  A plain list keeps
/// the profile table free of any HTTP client dependency: the transport copies
/// it into its own header map.
using UpstreamHeaderList = std::vector<std::pair<std::string, std::string>>;

/// Everything the proxy needs to know about one upstream sub-type.
///
/// The table in `upstream_profile_for_subtype()` is the single dispatch point:
/// SQL only reads `upstreams.subtype` verbatim, the routing snapshot resolves
/// it once to a pointer, and every runtime decision reads a field (or calls
/// the decorator) instead of comparing the sub-type string again.  Adding a
/// sub-type that needs proxy behaviour is one row here plus one
/// `AccountSubtypeSpec` row on the Python side.
struct UpstreamProfile {
    const char *name = "default";
    /// The gateway routes by session id and rejects clients that do not
    /// identify one; every attempt must carry `x-opencode-session`.
    bool needs_session = false;
    CooldownRecoveryChannel cooldown_recovery =
        CooldownRecoveryChannel::kHttpProbe;
    /// Never null: the default profile decorates nothing, so the transport
    /// calls this unconditionally instead of branching on a flag.
    void (*decorate_headers)(UpstreamHeaderList &, const std::string &session) =
        nullptr;
};

inline void decorate_no_upstream_headers(UpstreamHeaderList &,
                                         const std::string &) {}

/// opencode.ai Zen/Go: the gateway filters on the client User-Agent (which the
/// proxy never forwards) and routes by session id.
inline void decorate_opencode_go_headers(UpstreamHeaderList &out,
                                         const std::string &session) {
    if (!session.empty())
        out.emplace_back("x-opencode-session", session);
    out.emplace_back("User-Agent", "token-board/1.0");
}

inline const UpstreamProfile &default_upstream_profile() {
    static const UpstreamProfile profile{
        "default", false, CooldownRecoveryChannel::kHttpProbe,
        decorate_no_upstream_headers};
    return profile;
}

/// Resolve a raw `upstreams.subtype` value.  Empty means "ordinary upstream"
/// and is silent; an unknown non-empty value degrades to the default profile
/// with one warning per resolution so a typo can never silently attach
/// sub-type headers to an unrelated upstream.  This runs while a routing
/// snapshot is built, not per request.
inline const UpstreamProfile *upstream_profile_for_subtype(
    const std::string &subtype) {
    static const UpstreamProfile profiles[] = {
        {"opencode-go", true, CooldownRecoveryChannel::kUsageSnapshot,
         decorate_opencode_go_headers},
    };
    if (subtype.empty()) return &default_upstream_profile();
    for (const auto &profile : profiles) {
        if (subtype == profile.name) return &profile;
    }
    TB_LOG_WARN("[Upstream] unknown subtype '%s'; using the default profile\n",
                subtype.c_str());
    return &default_upstream_profile();
}

struct ForwardOptions;

/// Store the profile on an attempt and resolve its session value: a real
/// conversation passes its request-scoped id; callers without one (embeddings,
/// cooldown probe) pass an empty string and get a one-off random id when the
/// profile needs a session.
void apply_upstream_profile(ForwardOptions &opts,
                            const UpstreamProfile &profile,
                            const std::string &session);
