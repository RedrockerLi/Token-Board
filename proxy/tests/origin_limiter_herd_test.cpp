/// 04-C-10 regression: releasing one slot wakes one waiter, not the herd.
///
/// The limiter used to notify_all() on release, so every parked waiter woke,
/// failed to take the single free slot and went back to sleep.  The structural
/// counter here is voluntary context switches (getrusage ru_nvcsw) across the
/// window right after the release: one wakeup costs ~1-2 switches, a broadcast
/// costs one per waiter.  ru_nvcsw is the only counter that sees every thread
/// (/proc/self/status only reports the main thread's switches).
#include "origin_limiter.h"

#include <sys/resource.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

namespace {

long voluntary_switches() {
    struct rusage usage {};
    getrusage(RUSAGE_SELF, &usage);
    return usage.ru_nvcsw;
}

}  // namespace

int main() {
    OriginLimiter &limiter = OriginLimiter::instance();
    const std::string origin = "https://herd.example.com";

    // Fill the per-origin budget, then park waiters behind it.
    std::vector<OriginLimiter::Lease> held;
    for (int i = 0; i < 4096; ++i) {
        auto lease = limiter.acquire(origin, 0);
        if (!lease) break;
        held.push_back(std::move(lease));
    }
    if (held.empty()) {
        std::printf("FAIL (C-10): the limiter granted no lease at all\n");
        return 1;
    }
    std::printf("held %zu leases, parking waiters\n", held.size());

    const int waiters = 64;
    std::atomic<int> parked{0};
    std::vector<std::thread> threads;
    threads.reserve(waiters);
    for (int i = 0; i < waiters; ++i) {
        threads.emplace_back([&limiter, &origin, &parked] {
            parked.fetch_add(1);
            // A short timeout keeps the case quick; measured wakes happen
            // right after the release below.
            auto lease = limiter.acquire(origin, 400);
            (void)lease;
        });
    }
    while (parked.load() < waiters) std::this_thread::yield();
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    const long before = voluntary_switches();
    held.pop_back();  // release exactly one slot
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    const long after = voluntary_switches();
    const long switches = after - before;

    for (auto &thread : threads) thread.join();
    held.clear();

    std::printf("voluntary switches after one release: %ld "
                "(waiters=%d)\n", switches, waiters);
    // One wakeup costs ~1-2 switches; a broadcast costs one per waiter (w6
    // measured 129 pre-fix).  The bound is deliberately loose so scheduler
    // noise cannot flake it, yet far below a herd.
    if (switches > 20) {
        std::printf("FAIL (C-10): the release woke the herd\n");
        return 1;
    }
    std::printf("OK (C-10): one release woke one waiter\n");
    return 0;
}
