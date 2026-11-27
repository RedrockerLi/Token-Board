/// 04-C-01 regression: one affinity bind/resolve must not allocate.
///
/// w9 replaced the concatenated hash input and the per-call std::to_string /
/// std::vector locals with thread_local scratch storage, which took the count
/// from 3-4 allocations per request (short / long scope) to 0.  Allocations are
/// counted by overriding global operator new/delete inside this target only —
/// the first call still materialises the thread-local buffers, so the fixture
/// warms up once and then measures the delta of a real bind + resolve.
#include "session_affinity.h"

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <new>
#include <string>
#include <vector>

namespace {
std::atomic<size_t> g_allocations{0};
std::atomic<size_t> g_allocations_array{0};

void *allocate(std::size_t size) {
    g_allocations.fetch_add(1, std::memory_order_relaxed);
    if (void *p = std::malloc(size == 0 ? 1 : size)) return p;
    throw std::bad_alloc();
}
}  // namespace

void *operator new(std::size_t size) { return allocate(size); }
void *operator new[](std::size_t size) {
    g_allocations_array.fetch_add(1, std::memory_order_relaxed);
    return allocate(size);
}
void operator delete(void *p) noexcept { std::free(p); }
void operator delete[](void *p) noexcept { std::free(p); }
void operator delete(void *p, std::size_t) noexcept { std::free(p); }
void operator delete[](void *p, std::size_t) noexcept { std::free(p); }

int main() {
    SessionAffinity affinity;
    // The long scope exercises the shape w9 measured as 4 allocations.
    const std::string scope =
        "scope-with-a-longer-name-for-the-long-scope-case-0123456789";
    const std::string session = "session-0123456789abcdef0123456789abcdef";
    std::vector<int> slots{202};

    // Warm up: the first bind materialises the thread_local scratch buffers and
    // the map node, neither of which a warm request should re-allocate.
    for (int i = 0; i < 4; ++i) {
        affinity.bind(scope, session, 202);
        (void)affinity.bound_index(scope, session, slots);
    }

    const size_t before = g_allocations.load(std::memory_order_relaxed);
    affinity.bind(scope, session, 202);
    const auto bound = affinity.bound_index(scope, session, slots);
    const size_t delta = g_allocations.load(std::memory_order_relaxed) - before;

    std::printf("allocations for one warm bind+resolve: %zu "
                "(array news: %zu)\n", delta,
                g_allocations_array.load(std::memory_order_relaxed));
    if (!bound.has_value() || *bound != 0) {
        std::printf("FAIL (C-01): the binding did not resolve to slot index 0\n");
        return 1;
    }
    if (delta != 0) {
        std::printf("FAIL (C-01): a warm bind+resolve still allocates\n");
        return 1;
    }
    std::printf("OK (C-01): zero allocations on the warm path\n");
    return 0;
}
