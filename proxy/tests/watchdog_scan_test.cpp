/// 04-C-11 regression: the watchdog's per-round work follows the number of
/// active watches, not the number of downstream-started ("active stream")
/// watches, and a round with no watches never reaches expire_if_due().
///
/// The counters are the production-observable metrics added to
/// forward_watchdog_internal.h / watchdog_service_internal.h:
///   - watchdog_scanned_watches(): active watch entries assembled by a run()
///     round;
///   - watchdog_expire_calls(): expire_if_due() calls made by a round.
/// They are pure counters (no behaviour branch) and are read here as deltas
/// around one forced rebuild.  No timing metric is used: deadlines are placed
/// an hour ahead, so no expire is due and any difference is structural.
#include "transport_internal.h"

#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <thread>
#include <vector>

namespace {

constexpr int kWatches = 8;

std::uint64_t scanned_watches() {
    return tb_transport::watchdog_scanned_watches().load(
        std::memory_order_relaxed);
}

std::uint64_t expire_calls() {
    return tb_transport::watchdog_expire_calls().load(
        std::memory_order_relaxed);
}

void settle() {
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
}

/// Wait until the scan counter has advanced past `baseline` (one rebuild is
/// requested explicitly below), then let a coalescing slice finish.  Returns
/// the delta.  A bounded wait keeps a missing rebuild as a clean failure.
long long wait_for_scan(long long baseline) {
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (static_cast<long long>(scanned_watches()) <= baseline &&
           std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    settle();
    return static_cast<long long>(scanned_watches()) - baseline;
}

}  // namespace

int main() {
    auto &watchdog = tb_transport::ForwardWatchdog::instance();

    // ── Round with zero watches: expire_if_due must not be called. ───────
    settle();
    const std::uint64_t expires_before = expire_calls();
    const long long scanned_before =
        static_cast<long long>(scanned_watches());
    settle();
    assert(expire_calls() == expires_before);

    // ── Eight far-deadline watches: one rebuild scans all eight. ─────────
    std::vector<std::shared_ptr<tb_transport::ForwardWatch>> watches;
    watches.reserve(kWatches);
    const long long started_ms = tb_transport::now_ms();
    for (int i = 0; i < kWatches; ++i) {
        auto watch = std::make_shared<tb_transport::ForwardWatch>();
        watch->set_initial_deadlines(started_ms, 3600, 3600, 0);
        watches.push_back(watch);
        watchdog.add(watch);
    }
    const long long added_scan_delta = wait_for_scan(scanned_before);
    std::printf("watchdog scan delta after adding %d far-deadline watches: "
                "%lld\n", kWatches, added_scan_delta);
    std::fflush(stdout);
    assert(added_scan_delta >= kWatches);
    assert(added_scan_delta <= 2LL * kWatches);

    // ── Only one watch has committed downstream, the scan is still O(8). ─
    // If the round only walked "active streams", the delta would be ~1.
    watches.front()->mark_downstream_started();
    for (size_t i = 1; i < watches.size(); ++i)
        assert(!watches[i]->downstream_started.load(std::memory_order_relaxed));

    const long long before_forced = static_cast<long long>(scanned_watches());
    watchdog.changed();  // force exactly one rebuild of the watch set
    const long long forced_scan_delta = wait_for_scan(before_forced);
    std::printf("watchdog scan delta with 1/%d downstream_started: %lld\n",
                kWatches, forced_scan_delta);
    std::fflush(stdout);
    assert(forced_scan_delta >= kWatches);
    assert(forced_scan_delta <= 2LL * kWatches);
    assert(forced_scan_delta != 1);

    std::printf("watchdog scan is per active watch (not per active stream); "
                "zero-watch rounds made no expire call\n");
    return 0;
}
