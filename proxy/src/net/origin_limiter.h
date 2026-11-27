#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>

class OriginLimiter {
public:
    class Lease {
    public:
        Lease() = default;
        Lease(OriginLimiter *owner, std::string origin, int wait_ms)
            : owner_(owner), origin_(std::move(origin)), wait_ms_(wait_ms) {}
        Lease(const Lease &) = delete;
        Lease &operator=(const Lease &) = delete;
        Lease(Lease &&other) noexcept { *this = std::move(other); }
        Lease &operator=(Lease &&other) noexcept {
            if (this != &other) {
                release();
                owner_ = other.owner_;
                origin_ = std::move(other.origin_);
                wait_ms_ = other.wait_ms_;
                other.owner_ = nullptr;
            }
            return *this;
        }
        ~Lease() { release(); }
        explicit operator bool() const noexcept { return owner_ != nullptr; }
        int wait_ms() const noexcept { return wait_ms_; }
    private:
        void release() {
            if (owner_) owner_->release(origin_);
            owner_ = nullptr;
        }
        OriginLimiter *owner_ = nullptr;
        std::string origin_;
        int wait_ms_ = 0;
    };

    static OriginLimiter &instance() {
        static OriginLimiter limiter;
        return limiter;
    }

    Lease acquire(const std::string &origin, int timeout_ms) {
        const auto started = std::chrono::steady_clock::now();
        const auto deadline = started + std::chrono::milliseconds(timeout_ms);
        std::unique_lock<std::mutex> lock(mutex_);
        if (!available_locked(origin)) {
            // Park as an explicit waiter instead of sleeping on a broadcast:
            // a release hands the freed slot to exactly one waiting request,
            // so a saturated origin does not turn every completion into a
            // wake-up of the whole queue (C-10).
            Waiter waiter;
            waiter.origin = &origin;
            waiters_.push_back(&waiter);
            // Keeps the queue consistent if the wait throws; a grant already
            // erased this entry, which makes the removal a no-op.
            struct QueueEntry {
                std::list<Waiter *> *queue;
                Waiter *waiter;
                ~QueueEntry() { queue->remove(waiter); }
            } entry{&waiters_, &waiter};
            while (!waiter.granted) {
                if (waiter.cv.wait_until(lock, deadline) ==
                        std::cv_status::timeout &&
                    !waiter.granted) {
                    // Timed out before a grant: leave the queue.  grant_one()
                    // erases the waiter before granting, and both sides run
                    // under mutex_, so a grant cannot be lost here.
                    return {};
                }
            }
            // The grantor already reserved this request's slot.
        } else {
            reserve_locked(origin);
        }
        ++lease_count_;
        const int waited = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - started).count());
        lease_wait_ms_ += static_cast<std::uint64_t>(waited);
        return Lease(this, origin, waited);
    }

    std::uint64_t lease_count() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return lease_count_;
    }
    std::uint64_t lease_wait_ms() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return lease_wait_ms_;
    }
    std::size_t active() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return total_active_;
    }

private:
    /// One parked acquire.  Each waiter owns its condition variable so a
    /// release can grant the slot to exactly this request.
    struct Waiter {
        const std::string *origin = nullptr;
        std::condition_variable cv;
        bool granted = false;
    };

    bool available_locked(const std::string &origin) const {
        const auto found = active_.find(origin);
        const std::size_t active = found == active_.end() ? 0 : found->second;
        return total_active_ < total_limit_ && active < origin_limit_;
    }

    void reserve_locked(const std::string &origin) {
        ++total_active_;
        ++active_[origin];
    }

    /// Hand the single slot freed by one release to the first waiter that can
    /// use it; the other waiters stay parked instead of re-checking.
    void grant_one_locked() {
        for (auto it = waiters_.begin(); it != waiters_.end(); ++it) {
            Waiter *waiter = *it;
            if (!available_locked(*waiter->origin)) continue;
            waiters_.erase(it);
            reserve_locked(*waiter->origin);
            waiter->granted = true;
            // Notify under the lock: a waiter that timed out concurrently has
            // already removed itself, so this grant cannot be consumed by
            // anyone else.
            waiter->cv.notify_one();
            return;
        }
    }

    void release(const std::string &origin) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto found = active_.find(origin);
        if (found == active_.end() || found->second == 0) return;
        --found->second;
        --total_active_;
        if (found->second == 0) active_.erase(found);
        grant_one_locked();
    }

    mutable std::mutex mutex_;
    std::list<Waiter *> waiters_;
    std::unordered_map<std::string, std::size_t> active_;
    std::size_t total_active_ = 0;
    std::uint64_t lease_count_ = 0;
    std::uint64_t lease_wait_ms_ = 0;
    static constexpr std::size_t origin_limit_ = 512;
    static constexpr std::size_t total_limit_ = 512;
};
