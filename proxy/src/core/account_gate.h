#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <vector>

/// Per-key-slot concurrency gate + per-key-slot subscription quota cooldown.
///
/// Thread-safe, all state in memory (a plan's 5h cooldown is intentionally
/// not persisted — a proxy restart simply retries the upstream once).
///
/// Concurrency is tracked PER KEY SLOT (one upstream credential per slot): each
/// key of an account
/// gets its own `max_concurrency` budget, so one saturated key overflows to
/// the next.
///
/// Plan cooldown is ALSO PER KEY SLOT: an explicit quota-exhaustion 429 on one key of a plan account
/// cools down only that key, so the fallback loop can immediately move to the
/// next key of the same account (each key is a separate upstream
/// subscription — a rate limit on one does not rate-limit its siblings).
///
/// Slots are never reclaimed solely because they are old: a healthy long
/// stream must keep its reservation. Every successful acquire has a matching
/// release in the request paths, and client disconnect handling releases it.
class AccountGate {
public:
    enum class KeyAcquireResult {
        kAcquired,
        kConcurrencyFull,
        kSubscriptionCooldown,
    };

    static constexpr auto PLAN_COOLDOWN = std::chrono::hours(5);

    /// Atomically verify explicit subscription cooldown state and acquire a
    /// configured concurrency slot. Ordinary upstream failures never affect
    /// this result.
    KeyAcquireResult try_acquire_eligible(int key_slot_id, int max_per_key) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        if (in_cooldown_locked(state, key_slot_id,
                               std::chrono::steady_clock::now()))
            return KeyAcquireResult::kSubscriptionCooldown;
        if (max_per_key <= 0) return KeyAcquireResult::kAcquired;
        auto it = state.count.find(key_slot_id);
        if (it == state.count.end()) {
            state.count[key_slot_id] = 1;
            return KeyAcquireResult::kAcquired;
        }
        if (it->second >= max_per_key)
            return KeyAcquireResult::kConcurrencyFull;
        ++it->second;
        return KeyAcquireResult::kAcquired;
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

    /// Mark a plan key slot as provider-quota-exhausted: it cools down for
    /// 5 hours. Callers must only invoke this after observing the explicit
    /// upstream quota error envelope.
    /// Per-key: only this `key_slot_id` is affected — sibling keys of the
    /// same account stay usable so the fallback loop can switch to them.
    void mark_subscription_cooldown(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.cooldown_until[key_slot_id] =
            std::chrono::steady_clock::now() + PLAN_COOLDOWN;
    }

    /// Slots currently inside the 5h cooldown window (probe candidates).
    /// Ordinary upstream failures are not included because they never create
    /// process-local key state.
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
        return false;
    }
    std::array<Shard, kShardCount> shards_;
};
