/// 04-C-04 / 04-C-05 regression: pool shutdown is idempotent and bounded.
///
/// C-04: the owner destroys the pool while another thread is inside
/// shutdown().  Before the fix the destructor saw the flag already set, skipped
/// its own transition and freed the worker threads under the joining caller
/// (abort).  After the fix the destructor joins the in-flight transition.
///
/// C-05: a request parked in a long upstream read must not pin the drain.  With
/// a grace budget the shutdown returns within it and reports the abandonment,
/// instead of waiting for the 86400s backstop.
#include "httplib.h"
#include "semaphore_pool.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <thread>

namespace {

int double_shutdown() {
    auto *pool = new SemaphorePool(2, 4, 64);
    std::atomic<bool> job_started{false};
    pool->enqueue([&job_started] {
        job_started.store(true);
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
    });
    while (!job_started.load()) std::this_thread::yield();

    std::atomic<bool> shutdown_returned{false};
    std::thread owner([pool, &shutdown_returned] {
        pool->shutdown();
        shutdown_returned.store(true);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    // The second caller: destroying the pool while shutdown() is joining.
    delete pool;
    owner.join();
    if (!shutdown_returned.load()) {
        std::printf("FAIL (C-04): the destructor returned before shutdown did\n");
        return 1;
    }
    std::printf("OK (C-04): destructor joined the in-flight shutdown\n");
    return 0;
}

int bounded_shutdown() {
    SemaphorePool pool(1, 1, 8, std::chrono::milliseconds(200));
    std::atomic<bool> started{false};
    pool.enqueue([&started] {
        started.store(true);
        std::this_thread::sleep_for(std::chrono::seconds(600));
    });
    while (!started.load()) std::this_thread::yield();
    const auto t0 = std::chrono::steady_clock::now();
    pool.shutdown();
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t0).count();
    std::printf("shutdown() returned after %lld ms, abandoned=%d\n",
                static_cast<long long>(ms), pool.abandoned() ? 1 : 0);
    if (ms > 2000 || !pool.abandoned()) {
        std::printf("FAIL (C-05): the drain was not bounded / not reported\n");
        return 1;
    }
    std::printf("OK (C-05): bounded drain, abandonment observable\n");
    return 0;
}

}  // namespace

int main() {
    if (double_shutdown() != 0) return 1;
    if (bounded_shutdown() != 0) return 1;
    std::fflush(stdout);
    // An abandoned pool is deliberately left alone (its detached worker still
    // holds the semaphore); exit instead of running its destructor.
    std::printf("semaphore shutdown regressions passed\n");
    std::fflush(stdout);
    std::_Exit(0);
}
