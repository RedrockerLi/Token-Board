#pragma once

#include "account_gate.h"
#include "db.h"
#include "key_cost_ledger.h"
#include "routing_snapshot.h"

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

struct UpstreamCandidate {
    // Candidate instances are request-local, but the expensive account and
    // credential data belongs to the immutable routing snapshot.  Keep only
    // shared read-only references here so expanding one account to several
    // key slots does not copy base URLs, endpoint paths or auth policy per
    // attempt.
    std::shared_ptr<const Database::AccountInfo> account_ref;
    std::shared_ptr<const Database::KeySlot> key_slot_ref;
    // Null when this credential has no usage snapshot: the candidate then has
    // no window signals and is never treated as exhausted.
    std::shared_ptr<const routing::CredentialUsage> usage_ref;
    int key_slot_id = -1;
    std::shared_ptr<const std::string> upstream_model_ref;
    int priority_group = 0;

    const Database::AccountInfo &account() const { return *account_ref; }
    const std::string &key() const { return key_slot_ref->key_value; }
    const std::string &upstream_model() const { return *upstream_model_ref; }
    const routing::CredentialUsage *usage() const { return usage_ref.get(); }
};

// ── Usage-aware scheduling (upstreams with a usage snapshot) ─────────────
//
// A credential's windows yield "pressure": the quota that would expire unused
// if the key is not chosen before the window resets.  Higher pressure (more
// remaining quota per remaining second) leads, so expiring quota is spent
// first; among keys sharing a reset time this balances usage.  Pressures
// compare by integer cross-multiplication (a multiply is marginally cheaper
// than a floating-point division, and exactness comes free) with no
// tolerance band — strictly greater wins, exactly equal falls through —
// which keeps the ordering a strict weak ordering for std::stable_sort.
inline constexpr int kExhaustedPercent = 100;

// A snapshot older than this supplies no pressure signal: stale data must not
// drive window comparisons (two poller periods at the 300s cadence).
inline constexpr std::int64_t kUsageFreshSeconds = 600;

/// Wall-clock epoch seconds (the resets_at / observed_at domain).
inline std::int64_t wall_now_epoch() {
    return std::chrono::duration_cast<std::chrono::seconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

/// True while the usage snapshot is recent enough to drive scheduling and
/// cooldown-recovery decisions.
inline bool usage_snapshot_fresh(const routing::CredentialUsage *usage,
                                 std::int64_t now) {
    return usage != nullptr && usage->observed_at > 0 &&
           now - usage->observed_at <= kUsageFreshSeconds;
}

inline const routing::UsageWindow *find_usage_window(
    const routing::CredentialUsage *usage, const char *name) {
    if (usage == nullptr) return nullptr;
    for (const auto &window : usage->windows)
        if (window.name == name) return &window;
    return nullptr;
}

/// A credential is exhausted — and must not take new requests while any
/// non-exhausted candidate exists — when any reported window sits at 100%
/// with its trusted reset still ahead.  The verdict is derived per request
/// (never frozen into the snapshot), so a window whose reset passes reopens
/// the key without waiting for a config reload.  A 100% reading without a
/// reset stamp counts only while the snapshot is fresh: past data may not
/// declare exhaustion, but a confirmed one is sticky — stale or incomplete
/// data never lifts it, and a fresh observation or the trusted reset does.
inline bool candidate_usage_exhausted(const UpstreamCandidate &candidate,
                                      std::int64_t now) {
    const auto *usage = candidate.usage();
    if (usage == nullptr) return false;
    const bool fresh = usage_snapshot_fresh(usage, now);
    for (const auto &window : usage->windows) {
        if (window.percent < kExhaustedPercent) continue;
        if (window.resets_at > now) return true;
        if (window.resets_at == 0 && fresh) return true;
    }
    return false;
}

/// Earliest trusted reset among the windows that veto the credential; 0 when
/// none is known (those sort last among exhausted candidates).
inline std::int64_t candidate_exhausted_reset(
    const UpstreamCandidate &candidate, std::int64_t now) {
    const auto *usage = candidate.usage();
    if (usage == nullptr) return 0;
    const bool fresh = usage_snapshot_fresh(usage, now);
    std::int64_t earliest = 0;
    for (const auto &window : usage->windows) {
        if (window.percent < kExhaustedPercent) continue;
        const bool vetos = window.resets_at > now ||
            (window.resets_at == 0 && fresh);
        if (!vetos || window.resets_at == 0) continue;
        if (earliest == 0 || window.resets_at < earliest)
            earliest = window.resets_at;
    }
    return earliest;
}

enum class UsageWindowKind { kMonthly, kWeekly, kRolling };

inline const char *usage_window_name(UsageWindowKind kind) {
    switch (kind) {
        case UsageWindowKind::kMonthly: return "month";
        case UsageWindowKind::kWeekly: return "week";
        case UsageWindowKind::kRolling: return "5h";
    }
    return "";
}

struct WindowPressure {
    std::int64_t remaining = 0;  // percentage points left (>= 1)
    std::int64_t seconds = 0;    // seconds until the trusted reset (>= 1)
};

/// One window's pressure signal, or false when the window contributes none:
/// the snapshot is stale (unknown), the window is missing, its reset already
/// passed, it is already spent, or — for the rolling window — it has not
/// started (an idle window has no countdown, so nothing is expiring yet).
/// A window that cannot be judged is never guessed.
inline bool window_pressure(const UpstreamCandidate &candidate,
                            UsageWindowKind kind, std::int64_t now,
                            WindowPressure *out) {
    const auto *usage = candidate.usage();
    if (!usage_snapshot_fresh(usage, now)) return false;
    const auto *window = find_usage_window(usage, usage_window_name(kind));
    if (window == nullptr) return false;
    if (kind == UsageWindowKind::kRolling) {
        if (window->state != routing::WindowState::kStarted) return false;
    } else if (window->state == routing::WindowState::kUnknown) {
        return false;
    }
    if (window->percent >= kExhaustedPercent) return false;
    if (window->resets_at <= now) return false;
    out->remaining = 100 - window->percent;
    out->seconds = window->resets_at - now;
    return out->remaining > 0;
}

/// Recovery plan for an explicit quota-exhaustion 429: prefer the soonest
/// trusted reset among the exhausted windows, then the window the upstream
/// envelope named, then the PLAN_COOLDOWN fallback.  Evidence records which
/// window drove the deadline so the usage watch can tell a real reset from a
/// percentage that merely dipped below 100.
struct QuotaCooldownPlan {
    std::int64_t until_wall = 0;         // when the key may serve again
    std::string window;                  // evidence window ("" = none)
    std::int64_t evidence_resets_at = 0; // its reset stamp at marking time
};

inline QuotaCooldownPlan quota_cooldown_plan(
    const UpstreamCandidate &candidate, const std::string &limit_window,
    std::int64_t now) {
    QuotaCooldownPlan plan;
    const auto *usage = candidate.usage();
    if (usage_snapshot_fresh(usage, now)) {
        std::int64_t earliest = 0;
        const routing::UsageWindow *earliest_window = nullptr;
        for (const auto &window : usage->windows) {
            if (window.percent < kExhaustedPercent) continue;
            if (window.resets_at <= now) continue;
            if (earliest == 0 || window.resets_at < earliest) {
                earliest = window.resets_at;
                earliest_window = &window;
            }
        }
        if (earliest_window != nullptr) {
            plan.until_wall = earliest;
            plan.window = earliest_window->name;
            plan.evidence_resets_at = earliest;
            return plan;
        }
        if (!limit_window.empty()) {
            const auto *window =
                find_usage_window(usage, limit_window.c_str());
            if (window != nullptr && window->resets_at > now) {
                plan.until_wall = window->resets_at;
                plan.window = window->name;
                plan.evidence_resets_at = window->resets_at;
                return plan;
            }
        }
        // The envelope named no window and none reads as spent: still record
        // the most-used window's reset stamp.  The watch then waits for that
        // window to begin a new cycle instead of re-clearing on a percentage
        // that merely rounds below 100 — the cooldown duration itself falls
        // back to PLAN_COOLDOWN because no trusted recovery time exists.
        const routing::UsageWindow *busiest = nullptr;
        for (const auto &window : usage->windows) {
            if (window.resets_at <= now) continue;
            if (busiest == nullptr || window.percent > busiest->percent)
                busiest = &window;
        }
        if (busiest != nullptr) {
            plan.window = busiest->name;
            plan.evidence_resets_at = busiest->resets_at;
        }
    }
    plan.until_wall = now + std::chrono::duration_cast<std::chrono::seconds>(
                                AccountGate::PLAN_COOLDOWN)
                                .count();
    // Keep evidence an earlier branch already recorded; only an empty window
    // falls back to whatever the envelope named.
    if (plan.window.empty()) plan.window = limit_window;
    return plan;
}

/// Order the candidates for one request.
///
/// Priority groups keep their configured order (cheaper/higher-priority
/// groups first): balancing may only reorder inside a group, never steer a
/// request onto a more expensive upstream.  Inside a group, ascending:
///   1. exhausted last (keys whose limiting window is spent);
///   2. the session-bound key first (cache affinity wins over balancing;
///      exhaustion still outranks it — a bound key that is unusable is
///      skipped like any other);
///   3. month, week, 5h pressure compared directly; a candidate with a
///      signal beats one without, and equal pressures fall through;
///   4. least cumulative cost (the cost ledger; in-flight allocations are
///      already folded in) — the only stage a candidate without usage data
///      competes on;
///   5. the base order: the group rotated by `rr_offset`, so exact ties
///      spread across requests instead of pinning the lowest index.
/// The final stable partition lifts exhausted candidates of every group
/// behind all usable ones, ordering them by their earliest trusted reset —
/// when nothing else exists the soonest-recovering key is attempted first
/// (the provider stays authoritative; local data never fabricates a 429).
inline std::vector<size_t> candidate_order(
    const std::vector<UpstreamCandidate> &candidates,
    std::optional<size_t> bound_index, size_t rr_offset, std::int64_t now,
    const KeyCostLedger *ledger, const AccountGate *gate) {
    std::vector<size_t> order;
    if (candidates.empty()) return order;
    const auto cost_of = [&](const UpstreamCandidate &candidate) {
        if (ledger == nullptr) return 0.0;
        const int in_flight = gate ? gate->in_flight(candidate.key_slot_id) : 0;
        return ledger->effective_cost(candidate.key_slot_id, in_flight);
    };
    const auto before = [&](size_t lhs, size_t rhs) {
        const auto &a = candidates[lhs];
        const auto &b = candidates[rhs];
        const bool a_exhausted = candidate_usage_exhausted(a, now);
        const bool b_exhausted = candidate_usage_exhausted(b, now);
        if (a_exhausted != b_exhausted) return !a_exhausted;
        if (a_exhausted) {
            // Both are vetoed: try the one whose trusted reset arrives
            // first; an unknown reset sorts last.
            const auto a_reset = candidate_exhausted_reset(a, now);
            const auto b_reset = candidate_exhausted_reset(b, now);
            if (a_reset != b_reset) {
                if (a_reset == 0) return false;
                if (b_reset == 0) return true;
                return a_reset < b_reset;
            }
            const double a_cost = cost_of(a);
            const double b_cost = cost_of(b);
            if (a_cost != b_cost) return a_cost < b_cost;
            return false;
        }
        const bool a_bound = bound_index.has_value() && *bound_index == lhs;
        const bool b_bound = bound_index.has_value() && *bound_index == rhs;
        if (a_bound != b_bound) return a_bound;
        for (const auto kind : {UsageWindowKind::kMonthly,
                                UsageWindowKind::kWeekly,
                                UsageWindowKind::kRolling}) {
            WindowPressure pa, pb;
            const bool have_a = window_pressure(a, kind, now, &pa);
            const bool have_b = window_pressure(b, kind, now, &pb);
            if (have_a != have_b) return have_a;
            if (have_a) {
                // Remaining quota per remaining second, compared by integer
                // cross-multiplication (r_a / t_a vs r_b / t_b): a multiply
                // is marginally cheaper than a floating-point division and
                // exactness comes free.  Equal pressures fall through to the
                // next window (or to cumulative cost).
                const std::int64_t lhs = pa.remaining * pb.seconds;
                const std::int64_t rhs = pb.remaining * pa.seconds;
                if (lhs != rhs) return lhs > rhs;
            }
        }
        const double a_cost = cost_of(a);
        const double b_cost = cost_of(b);
        if (a_cost != b_cost) return a_cost < b_cost;
        return false;
    };
    size_t i = 0;
    while (i < candidates.size()) {
        const int group = candidates[i].priority_group;
        const size_t begin = i;
        while (i < candidates.size() &&
               candidates[i].priority_group == group) ++i;
        const size_t size = i - begin;
        std::vector<size_t> members(size);
        for (size_t k = 0; k < size; ++k) members[k] = begin + k;
        // Rotate the base order: stable_sort keeps it for ties, so exact
        // cost/pressure ties spread by request instead of by index.
        std::rotate(members.begin(), members.begin() + (rr_offset % size),
                    members.end());
        std::stable_sort(members.begin(), members.end(), before);
        order.insert(order.end(), members.begin(), members.end());
    }
    std::stable_partition(order.begin(), order.end(), [&](size_t index) {
        return !candidate_usage_exhausted(candidates[index], now);
    });
    return order;
}

inline bool candidate_failure_retryable(int status_code) {
    return status_code == 401 || status_code == 403 ||
           status_code == 429 || status_code >= 500;
}
