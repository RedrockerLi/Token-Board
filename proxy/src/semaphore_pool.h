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
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <functional>
#include <list>
#include <mutex>
#include <semaphore.h>
#include <thread>
#include <vector>

class SemaphorePool final : public httplib::TaskQueue {
  public:
    /// @param n      Initial number of worker threads (≥ 1).
    /// @param max_n  Hard ceiling for resize().
    explicit SemaphorePool(size_t n, size_t max_n = 512)
        : num_threads_(n), max_threads_(max_n) {
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
        {
            std::lock_guard<std::mutex> lock(mutex_);
            jobs_.push_back(std::move(fn));
        }
        // Post outside the lock: the woken thread can enter the critical
        // section immediately instead of blocking on `mutex_`.
        sem_post(&sem_);
        return true;
    }

    void shutdown() override {
        shutdown_.store(true, std::memory_order_release);

        // Post exactly N times — one per worker.
        // `sem_post` atomically increments the counter.  Workers that are
        // still executing a job will consume their post when they loop
        // back to `sem_wait`, so no post is lost.
        for (size_t i = 0; i < num_threads_; ++i) {
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
        if (new_size > max_threads_) new_size = max_threads_;
        if (new_size <= num_threads_) return;
        for (size_t i = num_threads_; i < new_size; ++i) {
            threads_.emplace_back(&SemaphorePool::worker, this);
        }
        num_threads_ = new_size;
    }

    // ── Monitoring ──────────────────────────────────────────────────────

    size_t size() const { return num_threads_; }
    size_t max_size() const { return max_threads_; }

  private:
    void worker() {
        for (;;) {
            // Wait for work (or shutdown signal).
            int rc = sem_wait(&sem_);
            if (rc == -1 && errno == EINTR) continue; // signal interrupt — retry

            // Check shutdown *before* touching the queue.
            if (shutdown_.load(std::memory_order_acquire)) return;

            std::function<void()> fn;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (!jobs_.empty()) {
                    fn = std::move(jobs_.front());
                    jobs_.pop_front();
                }
            }

            if (fn) {
                fn();
            } else {
                // spurious wakeup from shutdown post during init race
            }
        }
    }

    std::vector<std::thread> threads_;
    std::list<std::function<void()>> jobs_;
    std::mutex mutex_;
    sem_t sem_;
    size_t num_threads_;
    const size_t max_threads_;
    std::atomic<bool> shutdown_{false};
};
