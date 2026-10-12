#include "proxy_server.h"

#include <cassert>
#include <cstdint>
#include <iostream>
#include <utility>
#include <vector>

int main() {
    SessionAffinity affinity;
    const std::string scope_a = "local-key-1:openai_responses";
    const std::string scope_b = "local-key-2:openai_responses";
    const std::vector<int> slots{101, 202, 303};

    // A cold session is deterministic and does not depend on vector offsets.
    const auto first = affinity.preferred_index(scope_a, "session-a", slots);
    assert(first == affinity.preferred_index(scope_a, "session-a", slots));

    // A fallback that succeeds on slot 202 becomes the next request's first
    // choice, exactly the regression that hash(session) % N could not fix.
    affinity.bind(scope_a, "session-a", 202);
    assert(affinity.preferred_index(scope_a, "session-a", slots) == 1);
    affinity.bind(scope_a, "response_abc", 202);
    assert(affinity.preferred_index(scope_a, "response_abc", slots) == 1);

    // Bindings cannot cross local credentials / route scope boundaries.
    affinity.bind(scope_b, "session-a", 303);
    assert(affinity.preferred_index(scope_a, "session-a", slots) == 1);
    assert(affinity.preferred_index(scope_b, "session-a", slots) == 2);

    // Tier-5 routing: candidate_order rotates WITHIN each priority group; the
    // group containing the affinity-preferred candidate starts at it, other
    // groups start at a per-request round-robin offset.  Groups are still
    // tried in priority order (cheap first).
    std::vector<UpstreamCandidate> candidates(5);
    candidates[0].priority_group = 0;
    candidates[1].priority_group = 0;
    candidates[2].priority_group = 0;
    candidates[3].priority_group = 1;
    candidates[4].priority_group = 2;
    // preferred index 2 is in group 0 → that group rotates from key 2; the
    // size-1 lower groups are unaffected by rr_offset.
    assert((candidate_order(candidates, 2, 0) ==
            std::vector<size_t>{2, 0, 1, 3, 4}));

    // Two groups of two keys: preferred index 2 lies in group 1.  Group 0 is
    // still tried first and rotates by rr_offset (wear-leveling); group 1
    // starts at the preferred key regardless of rr_offset.
    std::vector<UpstreamCandidate> two_group(4);
    two_group[0].priority_group = 0;
    two_group[1].priority_group = 0;
    two_group[2].priority_group = 1;
    two_group[3].priority_group = 1;
    assert((candidate_order(two_group, 2, 0) ==
            std::vector<size_t>{0, 1, 2, 3}));
    assert((candidate_order(two_group, 2, 1) ==
            std::vector<size_t>{1, 0, 2, 3}));

    // ── V2.5 usage-aware scheduling ────────────────────────────────────
    // usage_ref is a per-credential usage snapshot (windows are
    // {percent, resets_at}); nullptr means "no usage data", which adds no
    // reordering of its own.
    auto usage_of = [](std::initializer_list<std::pair<int, std::int64_t>>
                           windows) {
        auto usage = std::make_shared<routing::CredentialUsage>();
        for (const auto &window : windows) {
            routing::UsageWindow entry;
            entry.percent = window.first;
            entry.resets_at = window.second;
            if (entry.percent >= kExhaustedPercent) usage->exhausted = true;
            usage->windows.push_back(entry);
        }
        return std::shared_ptr<const routing::CredentialUsage>(usage);
    };

    // The key whose usable quota resets soonest leads when it is at least
    // kUsageLeadGap (30 min) ahead of the next distinct reset: 10 min vs 2 h
    // wins, so the rotation starts at it instead of the affinity choice.
    std::vector<UpstreamCandidate> lead(3);
    lead[0].usage_ref = usage_of({{20, 7200}});
    lead[1].usage_ref = usage_of({{20, 600}});
    assert(usage_lead_start(lead, 0) == 1);
    assert((candidate_order(lead, usage_lead_start(lead, 0), 0) ==
            std::vector<size_t>{1, 2, 0}));

    // A window above kUsablePercent (70) has nothing worth spending early, so
    // it supplies no deadline; only windows <= 70 compete.
    std::vector<UpstreamCandidate> unusable(2);
    unusable[0].usage_ref = usage_of({{85, 600}});
    unusable[1].usage_ref = usage_of({{20, 7200}});
    assert(usage_lead_start(unusable, 0) == 0);

    // A 0% window supplies a deadline like any other usable window: the
    // percentage measures usage, it never tells whether the window is live
    // (under 1% rounds to 0 and the window is still counting down).
    std::vector<UpstreamCandidate> idle(2);
    idle[0].usage_ref = usage_of({{0, 7200}});
    idle[1].usage_ref = usage_of({{0, 600}});
    assert(candidate_usage_deadline(idle[1]) == 600);
    assert(usage_lead_start(idle, 0) == 1);

    // Gap gate: exactly 30 min leads, one second less does not.
    std::vector<UpstreamCandidate> gap(2);
    gap[0].usage_ref = usage_of({{20, 2400}});
    gap[1].usage_ref = usage_of({{20, 600}});
    assert(usage_lead_start(gap, 0) == 1);
    gap[0].usage_ref = usage_of({{20, 2399}});
    assert(usage_lead_start(gap, 0) == 0);

    // Ties (one shared deadline, or a single deadline-bearing candidate) leave
    // the affinity-preferred start untouched.
    std::vector<UpstreamCandidate> tied(2);
    tied[0].usage_ref = usage_of({{20, 600}});
    tied[1].usage_ref = usage_of({{40, 600}});
    assert(usage_lead_start(tied, 1) == 1);
    std::vector<UpstreamCandidate> single(2);
    single[0].usage_ref = usage_of({{20, 600}});
    assert(usage_lead_start(single, 1) == 1);

    // Exhausted candidates (any window >= 100) move to the END of the order
    // without being dropped; the relative order inside both halves is stable
    // and usage-free candidates keep their position.
    std::vector<UpstreamCandidate> mixed(4);
    mixed[0].usage_ref = usage_of({{100, 600}});
    mixed[1].usage_ref = usage_of({{20, 600}});
    mixed[2].usage_ref = usage_of({{100, 600}});
    assert((candidate_order(mixed, 0, 0) == std::vector<size_t>{1, 3, 0, 2}));
    assert(mixed[3].usage() == nullptr);

    // The partition spans priority groups: the whole point of a group order is
    // to pick the cheapest usable key, and here the cheap group's only key is
    // reported spent, so the paid group goes first.  A credential with an
    // exhausted window never leads, even when another window still has quota.
    std::vector<UpstreamCandidate> groups(2);
    groups[0].priority_group = 0;
    groups[0].usage_ref = usage_of({{100, 600}, {20, 600}});
    groups[1].priority_group = 1;
    assert(candidate_usage_deadline(groups[0]) == 0);
    assert(usage_lead_start(groups, 0) == 0);
    assert((candidate_order(groups, 0, 0) == std::vector<size_t>{1, 0}));

    // A credential with no usage data is ordered by the rotation alone.
    std::vector<UpstreamCandidate> legacy(3);
    legacy[0].priority_group = 0;
    legacy[1].priority_group = 0;
    legacy[2].priority_group = 0;
    assert(usage_lead_start(legacy, 0) == 0);
    assert((candidate_order(legacy, 2, 0) == std::vector<size_t>{2, 0, 1}));
    assert((candidate_order(legacy, 99, 1) == std::vector<size_t>{1, 2, 0}));

    // A binding to a key outside the supplied slots cannot return an
    // out-of-range index: preferred_index falls back to rendezvous within the
    // given slots.  Affinity domain is now cross-group, so a key bound in a
    // later group IS found when the full candidate set is supplied.
    affinity.bind(scope_a, "lower-fallback", 999);
    const auto top = affinity.preferred_index(
        scope_a, "lower-fallback", std::vector<int>{101, 202, 303});
    assert(top < 3);
    const std::vector<int> all_slots{101, 202, 303, 404, 505};
    affinity.bind(scope_a, "cross-session", 404);
    assert(affinity.preferred_index(scope_a, "cross-session", all_slots) == 3);

    // A revoked credential is local to one candidate; sibling keys must be
    // eligible for fallback just like rate-limit and provider failures.
    assert(candidate_failure_retryable(401));
    assert(candidate_failure_retryable(403));
    assert(candidate_failure_retryable(429));
    assert(candidate_failure_retryable(502));
    assert(!candidate_failure_retryable(400));

    // Eligibility distinguishes local capacity from the provider's explicit
    // subscription quota cooldown. Ordinary failures must not create a key
    // state that suppresses the next request.
    AccountGate gate;
    assert(gate.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    gate.release(101);
    gate.mark_subscription_cooldown(101);
    assert(gate.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kSubscriptionCooldown);
    gate.clear_cooldown(101);
    assert(gate.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kAcquired);
    assert(gate.try_acquire_eligible(101, 1) ==
           AccountGate::KeyAcquireResult::kConcurrencyFull);
    gate.release(101);

    // Cost-led cold start: with a ledger wired in, a session with no binding
    // prefers the key slot that has accrued the least cost; ties (all-zero)
    // resolve to the smallest index.  A binding still wins over ledger cost.
    KeyCostLedger cost_ledger;
    cost_ledger.add(202, 1.5);
    SessionAffinity cost_affinity(&cost_ledger);
    assert(cost_affinity.preferred_index(scope_a, "cold-session", slots) == 0);
    cost_ledger.add(101, 2.0);
    cost_ledger.add(303, 3.0);
    assert(cost_affinity.preferred_index(scope_a, "cold-session", slots) == 1);
    cost_affinity.bind(scope_a, "cold-session", 303);
    assert(cost_affinity.preferred_index(scope_a, "cold-session", slots) == 2);

    std::cout << "session affinity tests passed\n";
}
