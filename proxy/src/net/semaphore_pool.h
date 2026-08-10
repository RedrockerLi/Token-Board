#pragma once
/// SemaphorePool — A thread pool backed by a POSIX counting semaphore.
/// Implements httplib::TaskQueue so it can be used as a drop-in replacement
/// for httplib::ThreadPool.
///
///   SemaphorePool uses sem_post() / sem_wait().  The kernel atomically
///   decrements the semaphore counter and unblocks *exactly one* waiting
///   thread per post.  No thundering herd.
///
/// Supports resize() to grow at runtime (only up — workers never exit
/// until shutdown).

// NOTE: httplib.h must be #included before this header, with
//       CPPHTTPLIB_OPENSSL_SUPPORT defined if SSL is needed.

#include <atomic>
#include <array>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <exception>
#include <functional>
#include <list>
#include <mutex>
#include <semaphore.h>
#include <thread>
#include <vector>

#include "request_timing.h"
#include "core/logging.h"

class SemaphorePool final : public httplib::TaskQueue {
  public:
    /// @param n      Initial number of worker threads (≥ 1).
    /// @param max_n  Hard ceiling for resize().
    explicit SemaphorePool(size_t n, size_t max_n = 512,
                           size_t max_queue = 4096)
        : num_threads_(n), max_threads_(max_n), max_queue_(max_queue) {
        assert(n > 0);
        sem_init(&sem_, 0, 0); // process-local, initial value 0
        threads_.reserve(max_n);
        for (size_t i = 0; i < n; ++i) {
            threads_.emplace_back(&SemaphorePool::worker, this);
        }
    }

    SemaphorePool(const SemaphorePool &) = delete;
    SemaphorePool &operator=(const SemaphorePool &) = delete;

    ~SemaphorePool() override {
        if (!shutdown_.load(std::memory_order_acquire)) shutdown();
    }

    // ── TaskQueue interface (called by httplib::Server::process_and_close_socket) ──

    bool enqueue(std::function<void()> fn) override {
        bool should_grow = false;
        size_t grow_to = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (shutdown_.load(std::memory_order_acquire)) return false;
            if (jobs_.size() >= max_queue_) {
                rejected_.fetch_add(1, std::memory_order_relaxed);
                return false;
            }
            jobs_.push_back({std::move(fn), std::chrono::steady_clock::now()});
            queued_.fetch_add(1, std::memory_order_release);
            const size_t current = num_threads_.load(std::memory_order_acquire);
            const size_t busy = active_.load(std::memory_order_acquire);
            if (current < max_threads_ && jobs_.size() > current - std::min(current, busy)) {
                should_grow = true;
                grow_to = std::min(max_threads_, std::max(current + jobs_.size(), current * 2));
            }
        }
        // Growth happens on the enqueue that observes the backlog.  This
        // removes the old 200 ms control-loop delay from burst latency.
        if (should_grow) resize(grow_to);
        // Post outside the lock: the woken thread can enter the critical
        // section immediately instead of blocking on `mutex_`.
        sem_post(&sem_);
        return true;
    }

    void shutdown() override {
        bool expected = false;
        if (!shutdown_.compare_exchange_strong(
                expected, true, std::memory_order_acq_rel))
            return;

        // Post exactly N times — one per worker. Existing queued jobs keep
        // their own semaphore permits and are drained before a worker consumes
        // one of these shutdown permits. This mirrors httplib::ThreadPool:
        // accepted sockets are not silently abandoned during graceful stop.
        for (size_t i = 0; i < num_threads_.load(); ++i) {
            sem_post(&sem_);
        }

        for (auto &t : threads_) {
            if (t.joinable()) t.join();
        }
        threads_.clear();

        sem_destroy(&sem_);
    }

    // ── Dynamic sizing ─────────────────────────────────────────────────

    /// Grow the pool to @p new_size workers (only grows — never shrinks).
    /// Clamped to max_threads_.  New threads start immediately.
    void resize(size_t new_size) {
        std::lock_guard<std::mutex> resize_lock(resize_mutex_);
        if (shutdown_.load(std::memory_order_acquire)) return;
        if (new_size > max_threads_) new_size = max_threads_;
        const size_t old_size = num_threads_.load(std::memory_order_acquire);
        if (new_size <= old_size) return;
        for (size_t i = old_size; i < new_size; ++i) {
            threads_.emplace_back(&SemaphorePool::worker, this);
        }
        num_threads_.store(new_size, std::memory_order_release);
    }

    // ── Monitoring ──────────────────────────────────────────────────────

    size_t size() const { return num_threads_.load(std::memory_order_acquire); }
    size_t max_size() const { return max_threads_; }
    size_t queued() const noexcept {
        return queued_.load(std::memory_order_acquire);
    }
    size_t active() const noexcept {
        return active_.load(std::memory_order_acquire);
    }
    size_t rejected() const noexcept {
        return rejected_.load(std::memory_order_acquire);
    }
    std::int64_t queue_oldest_age_ms() noexcept {
        std::lock_guard<std::mutex> lock(mutex_);
        if (jobs_.empty()) return 0;
        return std::max<std::int64_t>(0, std::chrono::duration_cast<
            std::chrono::milliseconds>(std::chrono::steady_clock::now() -
                                       jobs_.front().enqueued_at).count());
    }
    double queue_average_ms() const noexcept {
        const auto count = queue_samples_.load(std::memory_order_acquire);
        return count ? static_cast<double>(queue_total_us_.load()) / count / 1000.0 : 0.0;
    }
    double queue_p95_ms() const noexcept {
        const auto count = queue_samples_.load(std::memory_order_acquire);
        if (!count) return 0.0;
        const auto target = (count * 95 + 99) / 100;
        std::uint64_t seen = 0;
        for (size_t i = 0; i < queue_histogram_.size(); ++i) {
            seen += queue_histogram_[i].load(std::memory_order_relaxed);
            if (seen >= target) return i == 0 ? 0.0 : static_cast<double>(1ULL << (i - 1));
        }
        return static_cast<double>(1ULL << (queue_histogram_.size() - 2));
    }

  private:
    struct Job {
        std::function<void()> fn;
        std::chrono::steady_clock::time_point enqueued_at;
    };

    void observe_queue_delay(std::chrono::steady_clock::duration delay) {
        const auto us = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(delay).count());
        queue_total_us_.fetch_add(us, std::memory_order_relaxed);
        queue_samples_.fetch_add(1, std::memory_order_release);
        std::uint64_t ms = us / 1000;
        size_t bucket = 0;
        while (ms && bucket + 1 < queue_histogram_.size()) {
            ++bucket;
            ms >>= 1;
        }
        queue_histogram_[bucket].fetch_add(1, std::memory_order_relaxed);
    }

    void worker() {
        for (;;) {
            // Wait for work (or shutdown signal).
            int rc = sem_wait(&sem_);
            if (rc == -1 && errno == EINTR) continue; // signal interrupt — retry

            Job job;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (!jobs_.empty()) {
                    job = std::move(jobs_.front());
                    jobs_.pop_front();
                    queued_.fetch_sub(1, std::memory_order_release);
                }
            }

            // During shutdown, process every job that was already accepted.
            // Once the queue is empty, this wakeup is one of the explicit
            // shutdown permits and the worker may exit.
            if (!job.fn && shutdown_.load(std::memory_order_acquire)) return;

            if (job.fn) {
                const auto queue_delay =
                    std::chrono::steady_clock::now() - job.enqueued_at;
                observe_queue_delay(queue_delay);
                set_request_queue_delay_ms(static_cast<int>(
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        queue_delay).count()));
                active_.fetch_add(1, std::memory_order_release);
                try {
                    job.fn();
                } catch (const std::exception &e) {
                    TB_LOG_ERROR("[Pool] uncaught request exception: %s\n",
                            e.what());
                } catch (...) {
                    TB_LOG_ERROR("[Pool] uncaught request exception\n");
                }
                set_request_queue_delay_ms(0);
                active_.fetch_sub(1, std::memory_order_release);
            } else {
                // spurious wakeup from shutdown post during init race
            }
        }
    }

    std::vector<std::thread> threads_;
    std::list<Job> jobs_;
    std::mutex mutex_;
    std::mutex resize_mutex_;
    sem_t sem_;
    std::atomic<size_t> num_threads_;
    const size_t max_threads_;
    const size_t max_queue_;
    std::atomic<bool> shutdown_{false};
    std::atomic<size_t> queued_{0};
    std::atomic<size_t> active_{0};
    std::atomic<size_t> rejected_{0};
    std::atomic<std::uint64_t> queue_total_us_{0};
    std::atomic<std::uint64_t> queue_samples_{0};
    std::array<std::atomic<std::uint64_t>, 32> queue_histogram_{};
};
