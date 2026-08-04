#pragma once

#include <chrono>
#include <cstdio>
#include <mutex>
#include <unordered_map>

/// Per-key-slot concurrency gate + per-account plan cooldown tracking.
///
/// Thread-safe, all state in memory (a plan's 5h cooldown is intentionally
/// not persisted — a proxy restart simply retries the upstream once).
///
/// Concurrency is tracked PER KEY SLOT (one `upstream_keys` row per slot, or
/// -1 for an account's legacy single key): each key of an account gets its
/// own `max_concurrency` budget, so one saturated key overflows to the next.
/// Plan cooldown stays PER ACCOUNT — a 429 on one key of a plan account
/// usually means the whole account is rate-limited, so they cool down
/// together.
///
/// Concurrency slots are reclaimed lazily after SLOT_TTL: normally every
/// acquire() is paired with release() when the request finishes, but a
/// streaming provider that is never invoked (client disconnected before
/// headers) would otherwise hold a slot forever.
class AccountGate {
public:
    static constexpr auto PLAN_COOLDOWN = std::chrono::hours(5);
    static constexpr auto SLOT_TTL = std::chrono::minutes(10);

    /// Try to occupy one concurrency slot for `key_slot_id`.
    /// `max_per_key` <= 0 means unlimited (slot always granted, nothing
    /// tracked). Returns true on success.
    bool acquire(int key_slot_id, int max_per_key) {
        if (max_per_key <= 0) return true;  // unlimited — no tracking
        auto now = std::chrono::steady_clock::now();
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = count_.find(key_slot_id);
        if (it != count_.end() && it->second.count > 0 &&
            now - it->second.first_ts > SLOT_TTL) {
            // Reclaim slots leaked by requests that never completed.
            fprintf(stderr, "[Gate] key slot %d: reclaiming %d stale "
                            "concurrency slot(s)\n",
                    key_slot_id, it->second.count);
            count_.erase(it);
            it = count_.end();
        }
        if (it == count_.end()) {
            count_[key_slot_id] = {1, now};
            return true;
        }
        if (it->second.count >= max_per_key) return false;
        ++it->second.count;
        return true;
    }

    /// Free one concurrency slot (must be balanced with a successful
    /// acquire). Safe to call for untracked / unlimited keys.
    void release(int key_slot_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = count_.find(key_slot_id);
        if (it != count_.end() && it->second.count > 0) {
            if (--it->second.count == 0) count_.erase(it);
        }
    }

    /// Mark a plan account as rate-limited: it cools down for 5 hours.
    void mark_cooldown(int account_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        cooldown_until_[account_id] =
            std::chrono::steady_clock::now() + PLAN_COOLDOWN;
    }

    /// True while the account is inside its 5h cooldown window.
    bool in_cooldown(int account_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = cooldown_until_.find(account_id);
        if (it == cooldown_until_.end()) return false;
        if (std::chrono::steady_clock::now() < it->second) return true;
        cooldown_until_.erase(it);  // expired — lazily cleaned up
        return false;
    }

private:
    struct Slots {
        int count = 0;
        std::chrono::steady_clock::time_point first_ts;
    };

    std::mutex mutex_;
    std::unordered_map<int, Slots> count_;
    std::unordered_map<int, std::chrono::steady_clock::time_point>
        cooldown_until_;
};
