#pragma once

#include <chrono>
#include <cstdio>
#include <mutex>
#include <unordered_map>

/// Per-key-slot concurrency gate + per-key-slot plan cooldown tracking.
///
/// Thread-safe, all state in memory (a plan's 5h cooldown is intentionally
/// not persisted — a proxy restart simply retries the upstream once).
///
/// Concurrency is tracked PER KEY SLOT (one `upstream_keys` row per slot, or
/// -account_id for an account's legacy single key): each key of an account
/// gets its own `max_concurrency` budget, so one saturated key overflows to
/// the next.
///
/// Plan cooldown is ALSO PER KEY SLOT: a 429 on one key of a plan account
/// cools down only that key, so the fallback loop can immediately move to the
/// next key of the same account (each key is a separate upstream
/// subscription — a rate limit on one does not rate-limit its siblings).
///
/// Slots are never reclaimed solely because they are old: a healthy long
/// stream must keep its reservation. Every successful acquire has a matching
/// release in the request paths, and client disconnect handling releases it.
class AccountGate {
public:
    static constexpr auto PLAN_COOLDOWN = std::chrono::hours(5);

    /// Try to occupy one concurrency slot for `key_slot_id`.
    /// `max_per_key` <= 0 means unlimited (slot always granted, nothing
    /// tracked). Returns true on success.
    bool acquire(int key_slot_id, int max_per_key) {
        if (max_per_key <= 0) return true;  // unlimited — no tracking
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = count_.find(key_slot_id);
        if (it == count_.end()) {
            count_[key_slot_id] = 1;
            return true;
        }
        if (it->second >= max_per_key) return false;
        ++it->second;
        return true;
    }

    /// Atomically verify cooldown state and acquire a concurrency slot.  A
    /// separate in_cooldown()+acquire() sequence permits a failure on another
    /// thread to start a cooldown between the two calls.
    bool try_acquire_eligible(int key_slot_id, int max_per_key) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (in_cooldown_locked(key_slot_id,
                               std::chrono::steady_clock::now()))
            return false;
        if (max_per_key <= 0) return true;
        auto it = count_.find(key_slot_id);
        if (it == count_.end()) {
            count_[key_slot_id] = 1;
            return true;
        }
        if (it->second >= max_per_key) return false;
        ++it->second;
        return true;
    }

    /// Free one concurrency slot (must be balanced with a successful
    /// acquire). Safe to call for untracked / unlimited keys.
    void release(int key_slot_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = count_.find(key_slot_id);
        if (it != count_.end() && it->second > 0) {
            if (--it->second == 0) count_.erase(it);
        }
    }

    /// Mark a plan key slot as rate-limited: it cools down for 5 hours.
    /// Per-key: only this `key_slot_id` is affected — sibling keys of the
    /// same account stay usable so the fallback loop can switch to them.
    void mark_cooldown(int key_slot_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        cooldown_until_[key_slot_id] =
            std::chrono::steady_clock::now() + PLAN_COOLDOWN;
    }

    /// Short circuit breaker for unusable/transient provider responses. Plan
    /// 429s use their contractual 5h cooldown above; 401/403/other 429/5xx and
    /// network failures back off 5s → 30s → 2min. A success clears it.
    void mark_failure(int key_slot_id, bool is_plan, int status_code) {
        if (is_plan && status_code == 429) { mark_cooldown(key_slot_id); return; }
        std::lock_guard<std::mutex> lock(mutex_);
        int &streak = failure_streak_[key_slot_id];
        ++streak;
        auto delay = streak == 1 ? std::chrono::seconds(5)
                   : streak == 2 ? std::chrono::seconds(30)
                                 : std::chrono::minutes(2);
        transient_cooldown_until_[key_slot_id] =
            std::chrono::steady_clock::now() + delay;
    }

    void mark_success(int key_slot_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        failure_streak_.erase(key_slot_id);
        transient_cooldown_until_.erase(key_slot_id);
    }

    /// True while the key slot is inside its 5h cooldown window.
    bool in_cooldown(int key_slot_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        return in_cooldown_locked(key_slot_id,
                                  std::chrono::steady_clock::now());
    }

private:
    bool in_cooldown_locked(
        int key_slot_id, std::chrono::steady_clock::time_point now) {
        auto it = cooldown_until_.find(key_slot_id);
        if (it != cooldown_until_.end()) {
            if (now < it->second) return true;
            cooldown_until_.erase(it);  // expired — lazily cleaned up
        }
        auto transient = transient_cooldown_until_.find(key_slot_id);
        if (transient == transient_cooldown_until_.end()) return false;
        if (now < transient->second) return true;
        transient_cooldown_until_.erase(transient);
        return false;
    }
    std::mutex mutex_;
    std::unordered_map<int, int> count_;
    std::unordered_map<int, std::chrono::steady_clock::time_point>
        cooldown_until_;
    std::unordered_map<int, std::chrono::steady_clock::time_point>
        transient_cooldown_until_;
    std::unordered_map<int, int> failure_streak_;
};
