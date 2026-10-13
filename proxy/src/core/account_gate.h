#pragma once

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

/// Per-key-slot concurrency gate + per-key-slot failure states.
///
/// Thread-safe, all state in memory: a single proxy serves one database (the
/// request-log spool holds an exclusive lock, see store/durable_spool.cpp),
/// and a restart simply re-learns every state from the upstream.
///
/// Concurrency is tracked PER KEY SLOT (one upstream credential per slot) and
/// counted for every slot, including `max_concurrency <= 0` (unlimited): the
/// cost ledger reads in_flight() so concurrent requests can see the
/// allocations already in progress instead of piling onto the same key.
///
/// Failure states are PER KEY SLOT and stay distinct by kind:
///   - subscription quota cooldown: an explicit quota-exhaustion 429 on a
///     plan key holds it until the trusted window reset (5h fallback when no
///     reset is known); only evidence (a fresh usage observation, or the
///     probe channel for keys without usage data) lifts it early.
///   - rate limit: an ordinary 429 holds the key for the upstream's
///     Retry-After (60s default); a later success clears it.
///   - auth error: 401/403 holds the key for 15 minutes; a later success
///     clears it.
///   - provider fault: 5xx/timeouts hold the key for 30 seconds; a later
///     success clears it.
/// Ordinary request-level failures (400-class) never create state.
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
        kRateLimited,
        kAuthError,
        kProviderFault,
    };

    static constexpr auto PLAN_COOLDOWN = std::chrono::hours(5);
    static constexpr auto RATE_LIMIT_AVOID = std::chrono::seconds(60);
    static constexpr auto AUTH_ERROR_AVOID = std::chrono::minutes(15);
    static constexpr auto PROVIDER_FAULT_AVOID = std::chrono::seconds(30);

    /// Evidence recorded when a quota cooldown is marked: which window's reset
    /// drove the deadline and that stamp at marking time.  The usage watch
    /// lifts the cooldown early only when the window has since begun a new
    /// cycle — a report that merely reads below 100% again (integer rounding)
    /// is not reset evidence.
    struct CooldownEvidence {
        std::string window;               // "5h" / "week" / "month" / ""
        std::int64_t resets_at = 0;       // wall epoch of that window's reset
        std::int64_t marked_wall = 0;     // wall epoch when the cooldown was set
    };

    /// Atomically verify the slot's failure states and acquire a concurrency
    /// slot.  Request-level upstream failures never affect this result.
    KeyAcquireResult try_acquire_eligible(int key_slot_id, int max_per_key) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        const auto now = std::chrono::steady_clock::now();
        if (in_cooldown_locked(state, key_slot_id, now))
            return KeyAcquireResult::kSubscriptionCooldown;
        if (const auto *avoid = avoid_locked(state, key_slot_id, now)) {
            switch (avoid->kind) {
                case AvoidKind::kRateLimit: return KeyAcquireResult::kRateLimited;
                case AvoidKind::kAuthError: return KeyAcquireResult::kAuthError;
                case AvoidKind::kProviderFault:
                    return KeyAcquireResult::kProviderFault;
            }
        }
        auto it = state.count.find(key_slot_id);
        const int current = it == state.count.end() ? 0 : it->second;
        if (max_per_key > 0 && current >= max_per_key)
            return KeyAcquireResult::kConcurrencyFull;
        if (it == state.count.end())
            state.count.emplace(key_slot_id, 1);
        else
            ++it->second;
        return KeyAcquireResult::kAcquired;
    }

    /// Free one concurrency slot (must be balanced with a successful
    /// acquire). Safe to call for untracked keys.
    void release(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        auto it = state.count.find(key_slot_id);
        if (it != state.count.end() && it->second > 0) {
            if (--it->second == 0) state.count.erase(it);
        }
    }

    /// Outstanding allocations for one slot — the "already assigned but not
    /// finished" count the routing ledger weighs when ordering candidates.
    int in_flight(int key_slot_id) const {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        auto it = state.count.find(key_slot_id);
        return it == state.count.end() ? 0 : it->second;
    }

    /// Mark a plan key slot as provider-quota-exhausted.  Callers must only
    /// invoke this after observing the explicit upstream quota error envelope
    /// and must pass the trusted recovery time when one is known (the reset
    /// of the exhausted window; PLAN_COOLDOWN is the fallback).  Per-key:
    /// sibling keys of the same account stay usable.
    void mark_subscription_cooldown(int key_slot_id,
                                    std::chrono::seconds duration,
                                    CooldownEvidence evidence) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.cooldown_until[key_slot_id] =
            std::chrono::steady_clock::now() + duration;
        state.cooldown_evidence[key_slot_id] = std::move(evidence);
    }

    void mark_rate_limited(int key_slot_id, std::chrono::seconds avoid) {
        mark_avoid(key_slot_id, AvoidKind::kRateLimit, avoid);
    }
    void mark_auth_error(int key_slot_id) {
        mark_avoid(key_slot_id, AvoidKind::kAuthError, AUTH_ERROR_AVOID);
    }
    void mark_provider_fault(int key_slot_id) {
        mark_avoid(key_slot_id, AvoidKind::kProviderFault, PROVIDER_FAULT_AVOID);
    }

    /// A completed successful call clears the short-lived avoid states.  The
    /// quota cooldown is never cleared here — only reset evidence may lift it
    /// (and a slot inside the cooldown is skipped, so it cannot succeed).
    void note_success(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.avoid.erase(key_slot_id);
    }

    /// Remaining seconds of the slot's active rate-limit avoid (0 = none):
    /// the executor reports the smallest value as the downstream Retry-After
    /// when every candidate was rate limited.
    int rate_limit_avoid_seconds(int key_slot_id) const {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        const auto now = std::chrono::steady_clock::now();
        auto it = state.avoid.find(key_slot_id);
        if (it == state.avoid.end() || it->second.kind != AvoidKind::kRateLimit ||
            now >= it->second.until)
            return 0;
        const auto remaining_ms = std::chrono::duration_cast<
            std::chrono::milliseconds>(it->second.until - now).count();
        return static_cast<int>(std::max<std::int64_t>(
            1, (remaining_ms + 999) / 1000));
    }

    /// Slots currently inside the quota cooldown window (probe candidates).
    /// Expired entries are erased lazily here.
    std::vector<int> cooling_keys(std::chrono::steady_clock::time_point now) {
        std::vector<int> out;
        for (auto &state : shards_) {
            std::lock_guard<std::mutex> lock(state.mutex);
            for (auto it = state.cooldown_until.begin();
                 it != state.cooldown_until.end();) {
                if (now < it->second) {
                    out.push_back(it->first);
                    ++it;
                } else {
                    state.cooldown_evidence.erase(it->first);
                    it = state.cooldown_until.erase(it);
                }
            }
        }
        return out;
    }

    /// Evidence recorded when the slot's active quota cooldown was marked;
    /// false when the slot is not cooling.
    bool cooldown_evidence(int key_slot_id, CooldownEvidence *out) const {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        auto it = state.cooldown_evidence.find(key_slot_id);
        if (it == state.cooldown_evidence.end()) return false;
        *out = it->second;
        return true;
    }

    /// Clear the quota cooldown for a slot (the recovery evidence confirmed
    /// the window reset, or the probe found the upstream serving again).
    /// No-op when the slot is not cooling.
    void clear_cooldown(int key_slot_id) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.cooldown_until.erase(key_slot_id);
        state.cooldown_evidence.erase(key_slot_id);
    }

private:
    enum class AvoidKind { kRateLimit, kAuthError, kProviderFault };

    struct Avoid {
        AvoidKind kind = AvoidKind::kRateLimit;
        std::chrono::steady_clock::time_point until;
    };

    struct Shard {
        std::mutex mutex;
        std::unordered_map<int, int> count;
        std::unordered_map<int, std::chrono::steady_clock::time_point>
            cooldown_until;
        std::unordered_map<int, CooldownEvidence> cooldown_evidence;
        std::unordered_map<int, Avoid> avoid;
    };

    static constexpr std::size_t kShardCount = 32;

    Shard &shard(int key_slot_id) const noexcept {
        const auto value = static_cast<std::uint32_t>(key_slot_id);
        return shards_[value % kShardCount];
    }

    void mark_avoid(int key_slot_id, AvoidKind kind,
                    std::chrono::seconds duration) {
        auto &state = shard(key_slot_id);
        std::lock_guard<std::mutex> lock(state.mutex);
        state.avoid[key_slot_id] = Avoid{
            kind, std::chrono::steady_clock::now() + duration};
    }

    static bool in_cooldown_locked(
        Shard &state, int key_slot_id,
        std::chrono::steady_clock::time_point now) {
        auto it = state.cooldown_until.find(key_slot_id);
        if (it != state.cooldown_until.end()) {
            if (now < it->second) return true;
            state.cooldown_until.erase(it);  // expired — lazily cleaned up
            state.cooldown_evidence.erase(key_slot_id);
        }
        return false;
    }

    static const Avoid *avoid_locked(Shard &state, int key_slot_id,
                                     std::chrono::steady_clock::time_point now) {
        auto it = state.avoid.find(key_slot_id);
        if (it == state.avoid.end()) return nullptr;
        if (now < it->second.until) return &it->second;
        state.avoid.erase(it);  // expired — lazily cleaned up
        return nullptr;
    }

    mutable std::array<Shard, kShardCount> shards_;
};

/// RAII companion for one acquired key slot: every path that acquires a slot
/// either completes normally (which releases) or passes through this guard,
/// so an exception inside an attempt callback can never strand the slot —
/// and the in-flight count the ledger reads stays honest.
class GateLease {
public:
    GateLease(AccountGate &gate, int key_slot_id) noexcept
        : gate_(&gate), key_slot_id_(key_slot_id) {}
    GateLease(const GateLease &) = delete;
    GateLease &operator=(const GateLease &) = delete;
    ~GateLease() { release(); }
    void release() {
        if (!gate_) return;
        gate_->release(key_slot_id_);
        gate_ = nullptr;
    }
    /// Disarm after another owner released the slot (e.g. complete()).
    void disarm() noexcept { gate_ = nullptr; }
private:
    AccountGate *gate_;
    int key_slot_id_;
};
