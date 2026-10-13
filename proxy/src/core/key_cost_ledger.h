#pragma once

#include <algorithm>
#include <cstddef>
#include <mutex>
#include <shared_mutex>
#include <unordered_map>
#include <vector>

/// Process-local ledger of accrued upstream cost per key slot, used to bias
/// in-group routing toward the least-worn key.
///
/// The totals are refreshed periodically from request_log through
/// Database::load_key_cost_totals: the priced, durable history the log still
/// retains is the source, so the ledger survives restarts instead of
/// resetting, and a request whose stored cost is zero while it did consume
/// tokens is never treated as zero consumption — the refresh folds such
/// tokens in at the global priced cost per token.  Request counts are never
/// used as a substitute for consumption.
///
/// Between refreshes, in-flight allocations count at the slot's expected
/// per-request cost so concurrent requests do not all choose the same
/// currently-cheapest key from the same snapshot.
///
/// Synchronization is a reader/writer lock because the split is asymmetric:
/// the request hot path reads `effective_cost` under a shared lock (many
/// readers run concurrently, none blocks another) while the refresh runs
/// under the exclusive lock from the probe thread.
class KeyCostLedger {
public:
    /// One slot's aggregates from request_log (retained window).
    struct SlotTotals {
        int key_slot_id = 0;
        double priced_cost = 0.0;       // Σ equivalent_cost over priced rows
        long long priced_tokens = 0;    // Σ total_tokens over priced rows
        long long unpriced_tokens = 0;  // Σ total_tokens over unpriced rows
        long long requests = 0;         // rows with tokens (per-request average)
    };

    /// Replace the accumulated totals.  Called from the probe thread on its
    /// own cadence; brand-new keys read as 0.0 and are preferred until they
    /// have served traffic.
    void refresh(const std::vector<SlotTotals> &totals) {
        double priced_cost_total = 0.0;
        long long priced_token_total = 0;
        long long request_total = 0;
        for (const auto &slot : totals) {
            priced_cost_total += slot.priced_cost;
            priced_token_total += slot.priced_tokens;
            request_total += slot.requests;
        }
        const double global_rate = priced_token_total > 0
            ? priced_cost_total / static_cast<double>(priced_token_total)
            : 0.0;
        std::unordered_map<int, Slot> next;
        double cost_total = 0.0;
        for (const auto &slot : totals) {
            if (slot.key_slot_id == 0) continue;
            Slot entry;
            entry.cost = slot.priced_cost +
                static_cast<double>(slot.unpriced_tokens) * global_rate;
            entry.per_request = slot.requests > 0
                ? entry.cost / static_cast<double>(slot.requests)
                : 0.0;
            cost_total += entry.cost;
            next.emplace(slot.key_slot_id, entry);
        }
        std::unique_lock<std::shared_mutex> lock(mutex_);
        slots_ = std::move(next);
        global_per_request_ = request_total > 0
            ? cost_total / static_cast<double>(request_total)
            : 0.0;
    }

    /// Ordering cost for one slot: accrued cost plus the outstanding
    /// allocations at their expected cost.  Ties (including an all-zero
    /// ledger) compare equal, so the caller's rotation decides.
    double effective_cost(int key_slot_id, int in_flight) const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        double cost = 0.0;
        double expected = global_per_request_;
        auto it = slots_.find(key_slot_id);
        if (it != slots_.end()) {
            cost = it->second.cost;
            if (it->second.per_request > 0.0)
                expected = it->second.per_request;
        }
        return cost + static_cast<double>(std::max(0, in_flight)) * expected;
    }

    /// Accrued (non-provisional) cost of one slot; 0.0 when unknown.
    double accrued_cost(int key_slot_id) const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        auto it = slots_.find(key_slot_id);
        return it == slots_.end() ? 0.0 : it->second.cost;
    }

    std::size_t slot_count() const {
        std::shared_lock<std::shared_mutex> lock(mutex_);
        return slots_.size();
    }

private:
    struct Slot {
        double cost = 0.0;
        double per_request = 0.0;
    };

    std::unordered_map<int, Slot> slots_;  // key_slot_id -> accrued totals
    double global_per_request_ = 0.0;
    mutable std::shared_mutex mutex_;
};
