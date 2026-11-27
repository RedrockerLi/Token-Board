/// 04-C-08 regression: one candidate_order() reads each candidate's ordering
/// cost exactly once, so its lock acquisitions are O(N), not O(N log N).
///
/// The comparator used to call cost_of() (AccountGate::in_flight under a
/// mutex + KeyCostLedger::effective_cost under a shared_mutex) on every
/// comparison.  For N=20 that measured 192-256 acquisitions per ordering; the
/// snapshot taken before the sort makes it exactly 2N: one gate mutex and one
/// ledger read lock per candidate.
///
/// Both acquisition points are counted through --wrap, which redirects the
/// executable's references to pthread_mutex_lock / pthread_rwlock_rdlock while
/// the real functions still run (each __wrap_* calls __real_*).  The test is
/// single-threaded, so the counter measures only candidate_order().
#include "candidate_selection.h"

#include <pthread.h>

#include <atomic>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace {

std::atomic<long long> g_mutex_locks{0};
std::atomic<long long> g_rwlock_rdlocks{0};

/// A candidate ref contains the snapshot-owned account/credential records;
/// only the fields candidate_order() reads need real values.
std::shared_ptr<const Database::AccountInfo> make_account(int id) {
    auto account = std::make_shared<Database::AccountInfo>();
    account->id = id;
    account->name = "account-" + std::to_string(id);
    account->base_url = "https://upstream.example.com";
    account->api_format = "openai";
    account->endpoint_path = "/v1/chat/completions";
    account->auth_header = "Authorization";
    account->max_concurrency = 0;
    return account;
}

std::shared_ptr<const Database::KeySlot> make_key_slot(int id) {
    auto slot = std::make_shared<Database::KeySlot>();
    slot->id = id;
    slot->key_value = "sk-lock-test-" + std::to_string(id);
    slot->position = id;
    return slot;
}

}  // namespace

extern "C" int __real_pthread_mutex_lock(pthread_mutex_t *mutex);
extern "C" int __wrap_pthread_mutex_lock(pthread_mutex_t *mutex) {
    g_mutex_locks.fetch_add(1, std::memory_order_relaxed);
    return __real_pthread_mutex_lock(mutex);
}

extern "C" int __real_pthread_rwlock_rdlock(pthread_rwlock_t *lock);
extern "C" int __wrap_pthread_rwlock_rdlock(pthread_rwlock_t *lock) {
    g_rwlock_rdlocks.fetch_add(1, std::memory_order_relaxed);
    return __real_pthread_rwlock_rdlock(lock);
}

int main() {
    constexpr int kCandidates = 20;
    constexpr long long kExpectedLocks = 2LL * kCandidates;

    auto upstream_model = std::make_shared<const std::string>("lock-test-model");
    std::vector<UpstreamCandidate> candidates;
    candidates.reserve(kCandidates);
    std::vector<KeyCostLedger::SlotTotals> totals;
    totals.reserve(kCandidates);
    for (int i = 0; i < kCandidates; ++i) {
        UpstreamCandidate candidate;
        candidate.key_slot_id = i + 1;
        candidate.account_ref = make_account(i + 1);
        candidate.key_slot_ref = make_key_slot(i + 1);
        candidate.usage_ref = nullptr;  // no window signal: cost decides
        candidate.upstream_model_ref = upstream_model;
        candidate.priority_group = 0;
        candidates.push_back(std::move(candidate));

        KeyCostLedger::SlotTotals slot;
        slot.key_slot_id = i + 1;
        // Descending by index, so a correct order is not the input order.
        slot.priced_cost = 1000.0 - static_cast<double>(i) * 10.0;
        slot.priced_tokens = 100;
        slot.unpriced_tokens = 0;
        slot.requests = 10;
        totals.push_back(slot);
    }

    KeyCostLedger ledger;
    ledger.refresh(totals);
    const AccountGate gate;
    const std::int64_t now = wall_now_epoch();

    // Expected: ascending effective cost (in-flight is 0 everywhere).
    std::vector<size_t> expected;
    expected.reserve(kCandidates);
    for (int i = kCandidates - 1; i >= 0; --i)
        expected.push_back(static_cast<size_t>(i));

    const auto order_once = [&]() {
        return candidate_order(candidates, std::nullopt, 0, now, &ledger,
                               &gate);
    };

    // Warm up allocator/sort paths before the measured call: only per-call
    // lock acquisitions may show up in the delta.
    for (int i = 0; i < 3; ++i) (void)order_once();

    g_mutex_locks.store(0, std::memory_order_relaxed);
    g_rwlock_rdlocks.store(0, std::memory_order_relaxed);
    const auto order = order_once();
    const long long mutex_locks = g_mutex_locks.load(std::memory_order_relaxed);
    const long long rwlock_rdlocks =
        g_rwlock_rdlocks.load(std::memory_order_relaxed);
    const long long total = mutex_locks + rwlock_rdlocks;

    std::printf("candidate_order N=%d locks: mutex=%lld rwlock=%lld total=%lld "
                "(expected %lld)\n",
                kCandidates, mutex_locks, rwlock_rdlocks, total,
                kExpectedLocks);
    std::fflush(stdout);

    // The regression is the amplifier: a pre-fix comparator exceeds 2N.
    assert(total == kExpectedLocks);
    assert(mutex_locks == kCandidates);          // AccountGate::in_flight
    assert(rwlock_rdlocks == kCandidates);       // KeyCostLedger::effective_cost

    // Same input, same cost snapshot -> identical ordering.
    assert(order.size() == static_cast<size_t>(kCandidates));
    assert(order == expected);
    const auto again = order_once();
    assert(again == order);

    // The order must follow the cost snapshot, not the declaration order.
    double previous = -1.0;
    for (size_t index : order) {
        const double cost = ledger.effective_cost(candidates[index].key_slot_id,
                                                  gate.in_flight(
                                                      candidates[index].key_slot_id));
        assert(cost >= previous);
        previous = cost;
    }

    std::printf("candidate_order ordering matches the cost snapshot; "
                "repeat call is identical\n");
    return 0;
}
