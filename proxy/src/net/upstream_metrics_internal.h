#pragma once

#include <atomic>
#include <cstdint>

// Transport counters are process-wide and intentionally live in the one
// metrics translation unit.  Forwarding code only records observations; it
// must not provide a second aggregation implementation.
namespace upstream_metrics {

extern std::atomic<std::uint64_t> dns_lookups;
extern std::atomic<std::uint64_t> dns_total_ms;
extern std::atomic<std::uint64_t> connect_total_ms;
extern std::atomic<std::uint64_t> tls_total_ms;
extern std::atomic<std::uint64_t> new_connections;
extern std::atomic<std::uint64_t> reused_connections;

}  // namespace upstream_metrics
