/// 04-C-17 regression: PooledLease's empty-lease reporting and self-move
/// guard.
///
/// connection_pool_internal.h is a fragment: transport_internal.h includes it
/// inside namespace tb_transport after origin/httplib includes.  This target
/// provides the few declarations the fragment needs (pool limits, clock,
/// transport metrics, OriginParts) instead of linking the production transport,
/// and then includes the real header so the tested code is the shipped one.
///
/// Asserts:
///   1. an empty lease's address() is the empty string (pre-fix it
///      dereferenced an empty optional and returned garbage / crashed);
///   2. after a self-move-assignment the lease still owns its client and
///      reports the original address (pre-fix release() returned the client to
///      the pool and silently emptied the lease).
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <list>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_set>
#include <utility>

// Production compiles cpp-httplib with TLS enabled; make_client() below the
// lease class calls the certificate-verification setters that support adds.
#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

namespace tb_transport {

// Necessary stubs for the fragment; values match origin_internal.h.
constexpr std::size_t CLIENT_POOL_MAX_IDLE = 256;
constexpr std::size_t CLIENT_POOL_MAX_IDLE_PER_ORIGIN = 64;
constexpr std::int64_t CLIENT_POOL_IDLE_TTL_MS = 60 * 1000;

inline long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct UpstreamClient {
    struct TransportMetrics {
        std::uint64_t pool_hits = 0;
        std::uint64_t pool_misses = 0;
        std::uint64_t clients_created = 0;
    };
};

struct OriginParts {
    std::string origin;
    std::string hostname;
};

#include "net/connection_pool_internal.h"

}  // namespace tb_transport

int main() {
    using tb_transport::PooledClient;
    using tb_transport::PooledLease;

    // 1. A default/empty lease reports nothing: address() is the empty
    //    string, client() is null and valid() is false.  This must not touch
    //    an empty optional.
    PooledLease empty{std::nullopt};
    std::printf("empty lease: valid=%d client=%p address='%s'\n",
                empty.valid() ? 1 : 0,
                static_cast<void *>(empty.client()), empty.address().c_str());
    std::fflush(stdout);
    assert(!empty.valid());
    assert(empty.client() == nullptr);
    assert(empty.address().empty());

    // 2. A held lease keeps its client across a self-move-assignment.
    auto client = std::make_unique<httplib::Client>("http://127.0.0.1:9");
    assert(client->is_valid());
    PooledClient pooled{std::move(client), "http://127.0.0.1:9", "127.0.0.1",
                        "127.0.0.1:9"};
    PooledLease lease{std::optional<PooledClient>{std::move(pooled)}};
    assert(lease.valid());
    assert(lease.client() != nullptr);
    assert(lease.address() == "127.0.0.1:9");

    // Route the assignment through a reference so this is a genuine self-move
    // without relying on a self-move diagnostic being disabled.
    PooledLease *alias = &lease;
    *alias = std::move(*alias);

    std::printf("self-moved lease: valid=%d client=%p address='%s'\n",
                lease.valid() ? 1 : 0,
                static_cast<void *>(lease.client()), lease.address().c_str());
    std::fflush(stdout);
    assert(lease.valid());
    assert(lease.client() != nullptr);
    assert(lease.address() == "127.0.0.1:9");

    std::printf("PooledLease empty-address and self-move guards hold\n");
    return 0;
}
