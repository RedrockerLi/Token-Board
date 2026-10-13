#include "proxy_server.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <utility>
#include <vector>

namespace {

routing::UsageWindow win(const char *name, int percent, std::int64_t resets,
                         routing::WindowState state =
                             routing::WindowState::kStarted) {
    routing::UsageWindow window;
    window.name = name;
    window.percent = percent;
    window.resets_at = resets;
    window.state = state;
    return window;
}

std::shared_ptr<const routing::CredentialUsage> usage_of(
    std::int64_t observed,
    std::initializer_list<routing::UsageWindow> windows) {
    auto usage = std::make_shared<routing::CredentialUsage>();
    usage->observed_at = observed;
    usage->windows.assign(windows.begin(), windows.end());
    return std::shared_ptr<const routing::CredentialUsage>(usage);
}

std::vector<size_t> order_of(const std::vector<UpstreamCandidate> &cands,
                             std::int64_t now,
                             std::optional<size_t> bound = std::nullopt,
                             size_t rr = 0, const KeyCostLedger *ledger = nullptr,
                             const AccountGate *gate = nullptr) {
    return candidate_order(cands, bound, rr, now, ledger, gate);
}

}  // namespace

int main() {
    const std::int64_t now = wall_now_epoch();
    const std::int64_t hour = 3600;
    const std::int64_t day = 24 * hour;

    // ── SessionAffinity: a binding store, nothing else ────────────────
    SessionAffinity affinity;
    const std::string scope_a = "local-key-1:openai_responses";
    const std::string scope_b = "local-key-2:openai_responses";
    const std::vector<int> slots{101, 202, 303};

    // No binding, empty session: no preference either way.
    assert(!affinity.bound_index(scope_a, "session-a", slots).has_value());
    assert(!affinity.bound_index(scope_a, "", slots).has_value());

    // A successful key becomes the session's bound key.
    affinity.bind(scope_a, "session-a", 202);
    assert(affinity.bound_index(scope_a, "session-a", slots) == 1);
    affinity.bind(scope_a, "response_abc", 202);
    assert(affinity.bound_index(scope_a, "response_abc", slots) == 1);

    // Bindings cannot cross local credentials / route scope boundaries.
    affinity.bind(scope_b, "session-a", 303);
    assert(affinity.bound_index(scope_a, "session-a", slots) == 1);
    assert(affinity.bound_index(scope_b, "session-a", slots) == 2);
    assert(!affinity.bound_index("other-scope", "session-a", slots).has_value());

    // A bound key outside this request's candidate set yields no preference.
    assert(!affinity.bound_index(scope_a, "session-a",
                                 std::vector<int>{404}).has_value());

    // ── Candidate ordering ────────────────────────────────────────────
    const auto fresh = [&](std::initializer_list<routing::UsageWindow> windows) {
        return usage_of(now, windows);
    };

    // Monthly pressure decides: with the same reset time, the key with more
    // remaining quota leads — a single percentage point is enough, because
    // the comparison is exact.
    std::vector<UpstreamCandidate> month(2);
    month[0].key_slot_id = 10;
    month[0].usage_ref = fresh({win("month", 20, now + 30 * day)});
    month[1].key_slot_id = 11;
    month[1].usage_ref = fresh({win("month", 21, now + 30 * day)});
    assert((order_of(month, now) == std::vector<size_t>{0, 1}));
    month[0].usage_ref = fresh({win("month", 21, now + 30 * day)});
    month[1].usage_ref = fresh({win("month", 20, now + 30 * day)});
    assert((order_of(month, now) == std::vector<size_t>{1, 0}));

    // Shorter time to reset raises pressure at equal quota.
    std::vector<UpstreamCandidate> urgency(2);
    urgency[0].key_slot_id = 10;
    urgency[0].usage_ref = fresh({win("month", 50, now + 50 * hour)});
    urgency[1].key_slot_id = 11;
    urgency[1].usage_ref = fresh({win("month", 50, now + 5 * hour)});
    assert((order_of(urgency, now) == std::vector<size_t>{1, 0}));

    // Exactly proportional pressures tie; the next stage (cumulative cost)
    // decides.  A: 50% left in 50s; B: 100% left in 100s — same burn rate.
    KeyCostLedger ledger;
    ledger.refresh({
        {10, 2.0, 1000, 0, 1},  // slot 10 already spent 2.0
        {11, 1.0, 1000, 0, 1},  // slot 11 spent 1.0
    });
    std::vector<UpstreamCandidate> proportional(2);
    proportional[0].key_slot_id = 10;
    proportional[0].usage_ref = fresh({win("month", 50, now + 50)});
    proportional[1].key_slot_id = 11;
    proportional[1].usage_ref = fresh({win("month", 0, now + 100)});
    assert((order_of(proportional, now, std::nullopt, 0, &ledger) ==
            std::vector<size_t>{1, 0}));

    // Windows are compared in the fixed order month, then week, then 5h:
    // only when the month stage cannot decide does the weekly window speak.
    std::vector<UpstreamCandidate> tiers(2);
    tiers[0].key_slot_id = 10;
    tiers[0].usage_ref = fresh({win("month", 40, now + 30 * day),
                                win("week", 10, now + 5 * day)});
    tiers[1].key_slot_id = 11;
    tiers[1].usage_ref = fresh({win("month", 40, now + 30 * day),
                                win("week", 60, now + 5 * day)});
    assert((order_of(tiers, now) == std::vector<size_t>{0, 1}));

    // The rolling window counts only when started: an idle window has no
    // countdown, so nothing is expiring yet and it supplies no signal.  A
    // started window with a real deadline does.
    UpstreamCandidate idle_cand, started_cand;
    idle_cand.key_slot_id = 10;
    idle_cand.usage_ref = fresh({win("5h", 0, now + 5 * hour,
                                     routing::WindowState::kIdle)});
    started_cand.key_slot_id = 11;
    started_cand.usage_ref = fresh({win("5h", 0, now + 2 * hour,
                                        routing::WindowState::kStarted)});
    WindowPressure pressure;
    assert(!window_pressure(idle_cand, UsageWindowKind::kRolling, now,
                            &pressure));
    assert(window_pressure(started_cand, UsageWindowKind::kRolling, now,
                           &pressure));
    assert(pressure.remaining == 100 && pressure.seconds == 2 * hour);
    std::vector<UpstreamCandidate> rolling{idle_cand, started_cand};
    assert((order_of(rolling, now) == std::vector<size_t>{1, 0}));

    // An unknown rolling state is never guessed into a signal.
    UpstreamCandidate unknown_cand;
    unknown_cand.key_slot_id = 12;
    unknown_cand.usage_ref = fresh({win("5h", 0, now + 2 * hour,
                                        routing::WindowState::kUnknown)});
    assert(!window_pressure(unknown_cand, UsageWindowKind::kRolling, now,
                            &pressure));

    // Stale snapshots supply no signals at all: cost decides instead.
    std::vector<UpstreamCandidate> stale(2);
    stale[0].key_slot_id = 10;
    stale[0].usage_ref = usage_of(now - kUsageFreshSeconds - 1,
                                  {win("month", 90, now + 30 * day)});
    stale[1].key_slot_id = 11;
    stale[1].usage_ref = usage_of(now - kUsageFreshSeconds - 1,
                                  {win("month", 10, now + 30 * day)});
    assert((order_of(stale, now, std::nullopt, 0, &ledger) ==
            std::vector<size_t>{1, 0}));
    // Boundary: exactly kUsageFreshSeconds still counts as fresh.
    stale[1].usage_ref = usage_of(now - kUsageFreshSeconds,
                                  {win("month", 10, now + 30 * day)});
    assert((order_of(stale, now) == std::vector<size_t>{1, 0}));

    // Exhaustion: any window at 100% with its trusted reset ahead vetoes the
    // key; it is scheduled last but never dropped, and exhausted keys queue
    // by their earliest trusted reset.  A soon-resetting sibling window does
    // not override the veto.
    std::vector<UpstreamCandidate> mixed(4);
    mixed[0].key_slot_id = 10;
    mixed[0].usage_ref = fresh({win("month", 100, now + 20 * day),
                                win("5h", 40, now + 60)});
    mixed[1].key_slot_id = 11;
    mixed[1].usage_ref = fresh({win("month", 20, now + 30 * day)});
    mixed[2].key_slot_id = 12;
    mixed[2].usage_ref = fresh({win("month", 100, now + 2 * day)});
    // 3 has no usage data and takes part in the rotation like any other.
    assert(candidate_usage_exhausted(mixed[0], now));
    assert(!candidate_usage_exhausted(mixed[1], now));
    assert((order_of(mixed, now) == std::vector<size_t>{1, 3, 2, 0}));

    // A window whose trusted reset has passed reopens the key (the 100%
    // reading belonged to the old cycle, so it supplies no pressure signal —
    // the next poll reports the fresh percentage); a 100% report without a
    // reset stamp counts only while the snapshot is fresh — stale data may
    // neither declare nor lift exhaustion.
    std::vector<UpstreamCandidate> reset(2);
    reset[0].key_slot_id = 10;
    reset[0].usage_ref = fresh({win("month", 100, now - 10)});
    reset[1].key_slot_id = 11;
    reset[1].usage_ref = fresh({win("month", 50, now + 30 * day)});
    assert(!candidate_usage_exhausted(reset[0], now));
    assert((order_of(reset, now) == std::vector<size_t>{1, 0}));
    UpstreamCandidate no_reset;
    no_reset.key_slot_id = 10;
    no_reset.usage_ref = fresh({win("month", 100, 0)});
    assert(candidate_usage_exhausted(no_reset, now));
    no_reset.usage_ref = usage_of(now - kUsageFreshSeconds - 1,
                                  {win("month", 100, 0)});
    assert(!candidate_usage_exhausted(no_reset, now));

    // The session-bound key leads its own group even against higher pressure
    // — cache affinity wins inside the group — but exhaustion still outranks
    // a binding, and groups keep their configured order.
    std::vector<UpstreamCandidate> bound(3);
    bound[0].key_slot_id = 10;  // group 0
    bound[0].priority_group = 0;
    bound[1].key_slot_id = 11;  // group 1, higher month pressure
    bound[1].priority_group = 1;
    bound[1].usage_ref = fresh({win("month", 10, now + 30 * day)});
    bound[2].key_slot_id = 12;  // group 1, bound
    bound[2].priority_group = 1;
    bound[2].usage_ref = fresh({win("month", 60, now + 30 * day)});
    assert((order_of(bound, now, /*bound=*/2) ==
            std::vector<size_t>{0, 2, 1}));
    // The same binding against an exhausted group-1 key: the veto holds.
    bound[2].usage_ref = fresh({win("month", 100, now + 30 * day)});
    assert((order_of(bound, now, /*bound=*/2) ==
            std::vector<size_t>{0, 1, 2}));

    // Exact ties keep the rotated base order: the request round-robin spreads
    // them instead of pinning the lowest index.
    std::vector<UpstreamCandidate> tied(3);
    tied[0].key_slot_id = 10;
    tied[1].key_slot_id = 11;
    tied[2].key_slot_id = 12;
    assert((order_of(tied, now, std::nullopt, 0) ==
            std::vector<size_t>{0, 1, 2}));
    assert((order_of(tied, now, std::nullopt, 1) ==
            std::vector<size_t>{1, 2, 0}));
    assert((order_of(tied, now, std::nullopt, 2) ==
            std::vector<size_t>{2, 0, 1}));

    // Candidates without usage data balance by cumulative cost alone; an
    // outstanding allocation raises a key's effective cost so concurrent
    // requests do not all pick the same cheapest key.
    AccountGate gate;
    std::vector<UpstreamCandidate> costed(2);
    costed[0].key_slot_id = 10;
    costed[1].key_slot_id = 11;
    KeyCostLedger wear;
    wear.refresh({
        {10, 0.7, 100, 0, 1},  // per-request 0.7
        {11, 0.4, 100, 0, 1},  // per-request 0.4
    });
    assert((order_of(costed, now, std::nullopt, 0, &wear, &gate) ==
            std::vector<size_t>{1, 0}));
    assert(gate.try_acquire_eligible(11, 0) ==
           AccountGate::KeyAcquireResult::kAcquired);
    // Slot 11 now carries one in-flight request at its expected cost 0.4:
    // 0.4 + 0.4 = 0.8 > slot 10's 0.7.
    assert((order_of(costed, now, std::nullopt, 0, &wear, &gate) ==
            std::vector<size_t>{0, 1}));
    gate.release(11);

    // Unpriced consumption is never zero: tokens on rows without a priced
    // cost fold in at the global priced cost per token.
    KeyCostLedger unpriced;
    unpriced.refresh({
        {10, 1.0, 1000, 0, 1},       // priced: 1.0 per 1000 tokens
        {11, 0.0, 0, 1000, 1},       // unpriced: 1000 tokens => 1.0
    });
    assert(unpriced.accrued_cost(11) > 0.99);
    assert(unpriced.accrued_cost(11) < 1.01);

    // ── Focused unit checks ───────────────────────────────────────────
    // A revoked credential is local to one candidate; sibling keys must be
    // eligible for fallback just like rate-limit and provider failures.
    assert(candidate_failure_retryable(401));
    assert(candidate_failure_retryable(403));
    assert(candidate_failure_retryable(429));
    assert(candidate_failure_retryable(502));
    assert(!candidate_failure_retryable(400));

    // The gate counts every slot's outstanding allocations, including
    // unlimited keys, so the ledger can see in-flight work.
    AccountGate counting;
    assert(counting.in_flight(101) == 0);
    assert(counting.try_acquire_eligible(101, 0) ==
           AccountGate::KeyAcquireResult::kAcquired);
    assert(counting.try_acquire_eligible(101, 0) ==
           AccountGate::KeyAcquireResult::kAcquired);
    assert(counting.in_flight(101) == 2);
    counting.release(101);
    counting.release(101);
    assert(counting.in_flight(101) == 0);
    assert(counting.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    assert(counting.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kConcurrencyFull);
    counting.release(101);

    // Failure states stay distinct per kind.  Quota cooldowns carry their
    // duration and evidence and are only lifted explicitly.
    AccountGate gate2;
    gate2.mark_subscription_cooldown(
        101, std::chrono::seconds(42),
        AccountGate::CooldownEvidence{"5h", 12345, 999});
    assert(gate2.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kSubscriptionCooldown);
    AccountGate::CooldownEvidence evidence;
    assert(gate2.cooldown_evidence(101, &evidence));
    assert(evidence.window == "5h" && evidence.resets_at == 12345 &&
           evidence.marked_wall == 999);
    // A success never clears the quota cooldown (a cooling slot is skipped
    // and cannot succeed), but it does clear the short-lived avoids.
    gate2.note_success(101);
    assert(gate2.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kSubscriptionCooldown);
    gate2.clear_cooldown(101);
    assert(gate2.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    gate2.release(101);

    gate2.mark_rate_limited(101, std::chrono::seconds(60));
    assert(gate2.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kRateLimited);
    assert(gate2.rate_limit_avoid_seconds(101) == 60);
    gate2.note_success(101);
    assert(gate2.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    gate2.release(101);

    gate2.mark_auth_error(201);
    assert(gate2.try_acquire_eligible(201, 1) ==
           AccountGate::KeyAcquireResult::kAuthError);
    gate2.mark_provider_fault(301);
    assert(gate2.try_acquire_eligible(301, 1) ==
           AccountGate::KeyAcquireResult::kProviderFault);
    gate2.note_success(201);
    gate2.note_success(301);
    assert(gate2.try_acquire_eligible(201, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    gate2.release(201);
    assert(gate2.try_acquire_eligible(301, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    gate2.release(301);

    // Cooling keys expose the evidence the usage watch needs.
    AccountGate gate3;
    gate3.mark_subscription_cooldown(
        101, std::chrono::seconds(30),
        AccountGate::CooldownEvidence{"month", 777, 111});
    auto cooling = gate3.cooling_keys(std::chrono::steady_clock::now());
    assert(cooling.size() == 1 && cooling[0] == 101);

    // Quota recovery planning prefers the soonest trusted reset, falls back
    // to the reset the upstream envelope named, then to the fixed cooldown.
    UpstreamCandidate quota_cand;
    quota_cand.key_slot_id = 10;
    quota_cand.usage_ref = fresh({win("month", 100, now + 20 * day),
                                  win("5h", 100, now + 2 * hour)});
    auto plan = quota_cooldown_plan(quota_cand, "month", now);
    assert(plan.until_wall == now + 2 * hour);
    assert(plan.window == "5h" && plan.evidence_resets_at == now + 2 * hour);
    quota_cand.usage_ref = fresh({win("month", 90, now + 20 * day),
                                  win("5h", 10, now + 2 * hour)});
    plan = quota_cooldown_plan(quota_cand, "5h", now);
    assert(plan.until_wall == now + 2 * hour && plan.window == "5h");
    // No exhausted window and no named window: the duration falls back to
    // PLAN_COOLDOWN, but the most-used window still supplies reset evidence
    // so the watch never clears on a percentage that merely dipped.
    plan = quota_cooldown_plan(quota_cand, "", now);
    assert(plan.until_wall ==
           now + std::chrono::duration_cast<std::chrono::seconds>(
                     AccountGate::PLAN_COOLDOWN)
                     .count());
    assert(plan.window == "month" &&
           plan.evidence_resets_at == now + 20 * day);
    // With no usable window at all there is nothing to wait for: the fixed
    // cooldown is the whole plan, and a newer observation may lift it.
    quota_cand.usage_ref = nullptr;
    plan = quota_cooldown_plan(quota_cand, "", now);
    assert(plan.window.empty() && plan.evidence_resets_at == 0);

    std::cout << "session affinity tests passed\n";
}
