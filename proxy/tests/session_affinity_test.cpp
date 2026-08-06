#include "proxy_server.h"

#include <cassert>
#include <iostream>
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

    // Cooldown eligibility and slot acquisition are one atomic gate decision.
    AccountGate gate;
    assert(gate.try_acquire_eligible(101, 1));
    gate.release(101);
    gate.mark_failure(101, account_types::CooldownClass::kTransient, 502);
    assert(!gate.try_acquire_eligible(101, 1));

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
