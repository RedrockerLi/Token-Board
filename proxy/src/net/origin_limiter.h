#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
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
        const auto available = [&] {
            return total_active_ < total_limit_ && active_[origin] < origin_limit_;
        };
        if (!cv_.wait_until(lock, deadline, available)) return {};
        ++total_active_;
        ++active_[origin];
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
    void release(const std::string &origin) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto found = active_.find(origin);
        if (found != active_.end() && found->second != 0) {
            --found->second;
            --total_active_;
            if (found->second == 0) active_.erase(found);
        }
        cv_.notify_one();
    }

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::unordered_map<std::string, std::size_t> active_;
    std::size_t total_active_ = 0;
    std::uint64_t lease_count_ = 0;
    std::uint64_t lease_wait_ms_ = 0;
    static constexpr std::size_t origin_limit_ = 64;
    static constexpr std::size_t total_limit_ = 256;
};
