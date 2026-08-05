#pragma once

#include <cstddef>
#include <shared_mutex>
#include <unordered_map>
#include <vector>

/// Process-local, in-memory ledger of accrued upstream cost per key slot,
/// used only to bias cold-session routing toward the least-used key.
///
/// Deliberately approximate: it is fed asynchronously by the accounting
/// thread (after a request-log entry is accepted), never persisted, and
/// resets on restart.  A brand-new key with no recorded cost reads as 0.0
/// and is preferred over any key that has served traffic.
///
/// Synchronization is a reader/writer lock because the split is asymmetric:
/// the request hot path reads `lowest_cost_index` under a shared lock (many
/// readers run concurrently, none blocks another) while the single accounting
/// thread writes under the exclusive lock.
class KeyCostLedger {
public:
    /// Add `cost` to `key_slot_id`'s running total.  Called from the
    /// accounting thread, off the request hot path.  Unknown slot id (0) and
    /// zero/NaN costs are ignored (`cost > 0.0` also rejects NaN).
    void add(int key_slot_id, double cost) {
        if (key_slot_id == 0 || !(cost > 0.0)) return;
        std::unique_lock<std::shared_mutex> lock(mutex_);
        spent_[key_slot_id] += cost;
    }

    /// Index (into `key_slot_ids`) of the least-cost slot; ties (including an
    /// all-zero ledger) resolve to the smallest index, so a cold-start choice
    /// is deterministic across identical ledgers.
    size_t lowest_cost_index(const std::vector<int> &key_slot_ids) const {
        if (key_slot_ids.empty()) return 0;
        std::shared_lock<std::shared_mutex> lock(mutex_);
        size_t best = 0;
        double best_cost = lookup_locked(key_slot_ids[0]);
        for (size_t i = 1; i < key_slot_ids.size(); ++i) {
            const double c = lookup_locked(key_slot_ids[i]);
            if (c < best_cost) {
                best = i;
                best_cost = c;
            }
        }
        return best;
    }

private:
    double lookup_locked(int id) const {
        auto it = spent_.find(id);
        return it == spent_.end() ? 0.0 : it->second;
    }

    std::unordered_map<int, double> spent_;  // key_slot_id -> accrued cost
    mutable std::shared_mutex mutex_;
};
