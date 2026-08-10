#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <vector>

/// Per-key-slot concurrency gate + per-key-slot plan cooldown tracking.
///
/// Thread-safe, all state in memory (a plan's 5h cooldown is intentionally
/// not persisted — a proxy restart simply retries the upstream once).
///
/// Concurrency is tracked PER KEY SLOT (one upstream credential per slot): each
/// key of an account
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
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        auto it = state.count.find(key_slot_id);
        if (it == state.count.end()) {
            state.count[key_slot_id] = 1;
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
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        if (in_cooldown_locked(state, key_slot_id,
                               std::chrono::steady_clock::now()))
            return false;
        if (max_per_key <= 0) return true;
        auto it = state.count.find(key_slot_id);
        if (it == state.count.end()) {
            state.count[key_slot_id] = 1;
            return true;
        }
        if (it->second >= max_per_key) return false;
        ++it->second;
        return true;
    }

    /// Free one concurrency slot (must be balanced with a successful
    /// acquire). Safe to call for untracked / unlimited keys.
    void release(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        auto it = state.count.find(key_slot_id);
        if (it != state.count.end() && it->second > 0) {
            if (--it->second == 0) state.count.erase(it);
        }
    }

    /// Mark a plan key slot as rate-limited: it cools down for 5 hours.
    /// Per-key: only this `key_slot_id` is affected — sibling keys of the
    /// same account stay usable so the fallback loop can switch to them.
    void mark_cooldown(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.cooldown_until[key_slot_id] =
            std::chrono::steady_clock::now() + PLAN_COOLDOWN;
    }

    /// Route a failed upstream attempt.  A genuine quota exhaustion on a
    /// subscription-class key (usage_limit=true, e.g. GoUsageLimitError) cools
    /// that key down (per-key); every other failure — including a plain
    /// transient 429 — backs off 5s → 30s → 2min instead of locking the key
    /// for the whole cooldown window.
    void record_failure(int key_slot_id, bool extended_usage_limit_cooldown,
                        bool usage_limit, int status_code) {
        if (usage_limit && extended_usage_limit_cooldown) {
            mark_cooldown(key_slot_id);
            return;
        }
        mark_failure(key_slot_id, status_code);
    }

    /// Short circuit breaker for unusable/transient provider responses.
    /// Non-quota failures back off 5s → 30s → 2min; a success clears it.  A
    /// genuine quota exhaustion is routed to mark_cooldown() by
    /// record_failure() — a transient 429 must never lock a key for 5h.
    void mark_failure(int key_slot_id, int status_code) {
        (void)status_code;  // reserved: error-code-graded backoff in a later phase
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        int &streak = state.failure_streak[key_slot_id];
        ++streak;
        auto delay = streak == 1 ? std::chrono::seconds(5)
                   : streak == 2 ? std::chrono::seconds(30)
                                 : std::chrono::minutes(2);
        state.transient_cooldown_until[key_slot_id] =
            std::chrono::steady_clock::now() + delay;
    }

    void mark_success(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.failure_streak.erase(key_slot_id);
        state.transient_cooldown_until.erase(key_slot_id);
    }

    /// True while the key slot is inside its 5h cooldown window.
    bool in_cooldown(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        return in_cooldown_locked(state, key_slot_id,
                                  std::chrono::steady_clock::now());
    }

    /// Slots currently inside the 5h cooldown window (probe candidates).
    /// Transient backoff (5s/30s/2min) is NOT included — it self-resolves in
    /// seconds and a probe would only race it.
    std::vector<int> cooling_keys(std::chrono::steady_clock::time_point now) {
        std::vector<int> out;
        for (auto &state : shards_) {
            std::lock_guard<std::mutex> lock(state.mutex);
            for (const auto &kv : state.cooldown_until)
                if (now < kv.second) out.push_back(kv.first);
        }
        return out;
    }

    /// Clear the 5h cooldown for a slot (the cooldown probe found the upstream
    /// healthy again).  No-op when the slot is not cooling.
    void clear_cooldown(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.cooldown_until.erase(key_slot_id);
    }

private:
    struct Shard {
        std::mutex mutex;
        std::unordered_map<int, int> count;
        std::unordered_map<int, std::chrono::steady_clock::time_point>
            cooldown_until;
        std::unordered_map<int, std::chrono::steady_clock::time_point>
            transient_cooldown_until;
        std::unordered_map<int, int> failure_streak;
    };

    static constexpr std::size_t kShardCount = 32;

    Shard &shard(int key_slot_id) noexcept {
        const auto value = static_cast<std::uint32_t>(key_slot_id);
        return shards_[value % kShardCount];
    }

    static bool in_cooldown_locked(
        Shard &state, int key_slot_id,
        std::chrono::steady_clock::time_point now) {
        auto it = state.cooldown_until.find(key_slot_id);
        if (it != state.cooldown_until.end()) {
            if (now < it->second) return true;
            state.cooldown_until.erase(it);  // expired — lazily cleaned up
        }
        auto transient = state.transient_cooldown_until.find(key_slot_id);
        if (transient == state.transient_cooldown_until.end()) return false;
        if (now < transient->second) return true;
        state.transient_cooldown_until.erase(transient);
        return false;
    }
    std::array<Shard, kShardCount> shards_;
};
