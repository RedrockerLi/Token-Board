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
///
/// Shutdown contract (see shutdown()):
///   - idempotent and mutually exclusive: a second caller waits for the first
///     transition to reach a terminal state instead of returning early, so the
///     owner can never destroy the pool under a caller that is still joining;
///   - graceful but bounded: accepted jobs are drained, then the pool waits at
///     most `shutdown_grace` for in-flight jobs.  Nothing is killed; when the
///     budget expires the pool reports itself abandoned (with counters) and the
///     owner must exit the process instead of destroying it.

// NOTE: httplib.h must be #included before this header, with
//       CPPHTTPLIB_OPENSSL_SUPPORT defined if SSL is needed.

#include <atomic>
#include <array>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <condition_variable>
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
    /// How long shutdown() waits for in-flight jobs before it stops waiting.
    /// The bound exists so a worker parked in an upstream read (the proxy's
    /// streaming backstop is 3600s) cannot hold the whole process in
    /// shutdown for an hour; it is a drain budget, not a kill switch.
    static constexpr std::chrono::milliseconds kDefaultShutdownGrace =
        std::chrono::seconds(30);

    /// @param n      Initial number of worker threads (≥ 1).
    /// @param max_n  Hard ceiling for resize().
    /// @param shutdown_grace  Drain budget for shutdown() (see above).
    explicit SemaphorePool(size_t n, size_t max_n = 512,
                           size_t max_queue = 4096,
                           std::chrono::milliseconds shutdown_grace =
                               kDefaultShutdownGrace)
        : num_threads_(n), max_threads_(max_n), max_queue_(max_queue),
          shutdown_grace_(shutdown_grace) {
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
        // Always participate in the shutdown: returning early because another
        // caller set the flag would free sem_/threads_ under that caller's
        // join.  shutdown() is idempotent and waits for their transition.
        shutdown();
        if (abandoned()) {
            TB_LOG_ERROR(
                "[Pool] destroyed while abandoned: detached workers still "
                "reference this pool's state; the process must exit instead of "
                "destroying it\n");
        }
    }

    // ── TaskQueue interface (called by httplib::Server::process_and_close_socket) ──

    bool enqueue(std::function<void()> fn) override {
        bool should_grow = false;
        size_t grow_to = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (stopping()) return false;
            if (jobs_.size() >= max_queue_) {
                rejected_.fetch_add(1, std::memory_order_relaxed);
                return false;
            }
            jobs_.push_back({std::move(fn), std::chrono::steady_clock::now()});
            queued_.fetch_add(1, std::memory_order_release);
            // Post while holding mutex_: shutdown() sets its flag and posts
            // the exit permits under the same mutex_, so this permit can
            // never surface after sem_destroy(), and shutdown always counts
            // every accepted job before it fixes the permit total.
            sem_post(&sem_);
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
        return true;
    }

    /// Stop accepting work, drain what was accepted, and stop waiting once
    /// `shutdown_grace` expires.
    ///
    /// Idempotent and mutually exclusive: the caller that wins the state
    /// transition owns the join; any other caller (a second thread, or the
    /// destructor) waits until that transition reaches a terminal state before
    /// returning, so it can never observe a half-torn-down pool.
    void shutdown() override {
        bool owner = false;
        Lifecycle observed = Lifecycle::kRunning;
        {
            // Serialize with resize(): no worker may be spawned after the
            // permit total below is fixed, and threads_/num_threads_ are
            // mutated only under this lock.
            std::lock_guard<std::mutex> resize_lock(resize_mutex_);
            std::lock_guard<std::mutex> lock(mutex_);
            Lifecycle expected = Lifecycle::kRunning;
            if (lifecycle_.compare_exchange_strong(
                    expected, Lifecycle::kStopping,
                    std::memory_order_acq_rel)) {
                owner = true;
                // Post exactly N times — one per worker. Existing queued jobs
                // already hold their own permits (posted in enqueue() under
                // mutex_) and are drained before a worker consumes one of these
                // shutdown permits. This mirrors httplib::ThreadPool: accepted
                // sockets are not silently abandoned during graceful stop.
                for (size_t i = 0;
                     i < num_threads_.load(std::memory_order_acquire); ++i) {
                    sem_post(&sem_);
                }
            }
            observed = lifecycle_.load(std::memory_order_acquire);
        }
        if (!owner) {
            // Another caller already owns the transition.  Returning here
            // would let this caller (typically the destructor) free the state
            // the owner is still joining, so wait for it instead.
            if (observed == Lifecycle::kStopping) wait_for_terminal();
            return;
        }
        drain_and_finish();
    }

    // ── Dynamic sizing ─────────────────────────────────────────────────

    /// Grow the pool to @p new_size workers (only grows — never shrinks).
    /// Clamped to max_threads_.  New threads start immediately.
    void resize(size_t new_size) {
        std::lock_guard<std::mutex> resize_lock(resize_mutex_);
        if (stopping()) return;
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
    /// True once shutdown() stopped waiting for in-flight jobs.  The pool must
    /// then be left alone (its detached workers still reference it) and the
    /// owner must exit the process.
    bool abandoned() const noexcept {
        return lifecycle_.load(std::memory_order_acquire) ==
               Lifecycle::kAbandoned;
    }
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
    /// Coarse shutdown state.  kStopping is owned by exactly one caller; the
    /// terminal states publish completion to every other caller.
    enum class Lifecycle { kRunning, kStopping, kStopped, kAbandoned };

    struct Job {
        std::function<void()> fn;
        std::chrono::steady_clock::time_point enqueued_at;
    };

    bool stopping() const noexcept {
        return lifecycle_.load(std::memory_order_acquire) !=
               Lifecycle::kRunning;
    }

    /// Block until the shutdown owner publishes a terminal state.
    void wait_for_terminal() {
        std::unique_lock<std::mutex> lock(done_mutex_);
        done_cv_.wait(lock, [this] {
            const auto state = lifecycle_.load(std::memory_order_acquire);
            return state == Lifecycle::kStopped ||
                   state == Lifecycle::kAbandoned;
        });
    }

    void publish_terminal(Lifecycle terminal) {
        // Notify while holding the lock: a waiter that observes the terminal
        // state may be the destructor, and it must not be able to destroy
        // done_cv_ before this notify_all() has returned.
        std::lock_guard<std::mutex> lock(done_mutex_);
        lifecycle_.store(terminal, std::memory_order_release);
        done_cv_.notify_all();
    }

    /// Join the drained workers, or abandon the pool when the drain budget
    /// expires.  Runs outside mutex_/resize_mutex_ because workers need mutex_
    /// to drain the queue; the shutdown flag is set, so resize() can only
    /// observe it and return and threads_ is stable from here on.
    void drain_and_finish() {
        const size_t expected_workers =
            num_threads_.load(std::memory_order_acquire);
        const auto started = std::chrono::steady_clock::now();
        bool drained = false;
        {
            std::unique_lock<std::mutex> lock(exit_mutex_);
            drained = exit_cv_.wait_until(
                lock, started + shutdown_grace_, [this, expected_workers] {
                    return exited_.load(std::memory_order_acquire) >=
                           expected_workers;
                });
        }
        if (drained) {
            for (auto &t : threads_) {
                if (t.joinable()) t.join();
            }
            threads_.clear();
            sem_destroy(&sem_);
            publish_terminal(Lifecycle::kStopped);
            return;
        }
        const auto elapsed_ms =
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - started).count();
        const size_t exited = exited_.load(std::memory_order_acquire);
        const size_t running = exited < expected_workers
                                   ? expected_workers - exited
                                   : 0;
        // Nothing is killed and nothing is torn down: the counters make the
        // dropped graceful stop observable, and the owner must exit the process
        // rather than destroy a pool whose detached workers still use sem_.
        TB_LOG_ERROR(
            "[Pool] graceful shutdown did not drain within %lld ms: %zu of %zu "
            "workers still running, %zu queued, %zu active; abandoning the "
            "pool (in-flight requests keep running, nothing is killed)\n",
            static_cast<long long>(elapsed_ms), running, expected_workers,
            queued_.load(std::memory_order_acquire),
            active_.load(std::memory_order_acquire));
        for (auto &t : threads_) {
            if (t.joinable()) t.detach();
        }
        threads_.clear();
        publish_terminal(Lifecycle::kAbandoned);
    }

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
            if (!job.fn && stopping()) {
                exited_.fetch_add(1, std::memory_order_release);
                exit_cv_.notify_all();
                return;
            }

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
    const std::chrono::milliseconds shutdown_grace_;
    std::atomic<Lifecycle> lifecycle_{Lifecycle::kRunning};
    /// Serializes waiters against the shutdown owner's terminal transition.
    std::mutex done_mutex_;
    std::condition_variable done_cv_;
    /// Tracks workers that returned from the loop, so the owner can wait for
    /// the drain within a deadline instead of joining blindly.
    std::mutex exit_mutex_;
    std::condition_variable exit_cv_;
    std::atomic<size_t> exited_{0};
    std::atomic<size_t> queued_{0};
    std::atomic<size_t> active_{0};
    std::atomic<size_t> rejected_{0};
    std::atomic<std::uint64_t> queue_total_us_{0};
    std::atomic<std::uint64_t> queue_samples_{0};
    std::array<std::atomic<std::uint64_t>, 32> queue_histogram_{};
};

/// cpp-httplib owns the TaskQueue returned by Server::new_task_queue().  The
/// production pool is owned by main(), so the object handed to httplib must
/// only forward calls and must never delete the pool itself.
class NonOwningTaskQueueProxy final : public httplib::TaskQueue {
  public:
    explicit NonOwningTaskQueueProxy(SemaphorePool &pool) : pool_(&pool) {}

    bool enqueue(std::function<void()> fn) override {
        return pool_->enqueue(std::move(fn));
    }

    void shutdown() override { pool_->shutdown(); }

  private:
    SemaphorePool *pool_;
};
