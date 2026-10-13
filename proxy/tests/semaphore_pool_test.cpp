// SemaphorePool lifecycle regression: producers enqueue (and trigger resize)
// while the main thread shuts the pool down.  A missing shutdown permit used
// to leave a worker blocked in sem_wait forever, deadlocking shutdown()'s
// join; the watchdog turns that hang into a process-level failure, and the
// accepted/executed counters catch any silently dropped job.
#include "httplib.h" // TaskQueue base class
#include "semaphore_pool.h"

#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <thread>
#include <unistd.h>
#include <vector>

namespace {

constexpr int kRounds = 60;
constexpr int kProducers = 4;
constexpr int kShutdownTimeoutMs = 10'000;

} // namespace

int main() {
    for (int round = 0; round < kRounds; ++round) {
        SemaphorePool pool(2, 8, 256);
        std::atomic<long> accepted{0};
        std::atomic<long> executed{0};
        std::atomic<bool> stop{false};

        std::vector<std::thread> producers;
        for (int i = 0; i < kProducers; ++i) {
            producers.emplace_back([&] {
                while (!stop.load(std::memory_order_relaxed)) {
                    const bool ok = pool.enqueue([&executed] {
                        // Hold a worker briefly so the backlog grows and the
                        // enqueue path actually calls resize().
                        std::this_thread::sleep_for(
                            std::chrono::microseconds(200));
                        executed.fetch_add(1, std::memory_order_relaxed);
                    });
                    if (ok)
                        accepted.fetch_add(1, std::memory_order_relaxed);
                    std::this_thread::yield();
                }
            });
        }

        std::atomic<bool> shutdown_done{false};
        std::thread watchdog([&] {
            const auto deadline = std::chrono::steady_clock::now() +
                std::chrono::milliseconds(kShutdownTimeoutMs);
            while (!shutdown_done.load(std::memory_order_acquire) &&
                   std::chrono::steady_clock::now() < deadline) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            if (!shutdown_done.load(std::memory_order_acquire)) {
                std::printf("FAIL: shutdown() did not complete (round %d)\n",
                            round);
                std::fflush(stdout);
                ::_exit(1);
            }
        });

        // Shut the pool down while the producers are still hammering it.
        pool.shutdown();
        shutdown_done.store(true, std::memory_order_release);
        stop.store(true, std::memory_order_relaxed);
        for (auto &producer : producers)
            producer.join();
        watchdog.join();

        // Once shut down, no further work is accepted...
        assert(!pool.enqueue([] {}));
        // ...and every accepted job ran exactly once: shutdown drains the
        // queue before any worker consumes an exit permit.
        assert(executed.load() == accepted.load());
    }
    std::printf("semaphore pool tests passed\n");
    return 0;
}
