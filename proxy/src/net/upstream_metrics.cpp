#include "transport_internal.h"

namespace upstream_metrics {

std::atomic<std::uint64_t> dns_lookups{0};
std::atomic<std::uint64_t> dns_total_ms{0};
std::atomic<std::uint64_t> connect_total_ms{0};
std::atomic<std::uint64_t> tls_total_ms{0};
std::atomic<std::uint64_t> new_connections{0};
std::atomic<std::uint64_t> reused_connections{0};

}  // namespace upstream_metrics

namespace upstream_metrics_detail {
UpstreamClient::TransportMetrics pool_metrics();
}  // namespace upstream_metrics_detail

UpstreamClient::TransportMetrics UpstreamClient::transport_metrics() {
    auto metrics = upstream_metrics_detail::pool_metrics();
    metrics.dns_lookups = upstream_metrics::dns_lookups.load(std::memory_order_relaxed);
    metrics.dns_total_ms = upstream_metrics::dns_total_ms.load(std::memory_order_relaxed);
    metrics.connect_total_ms = upstream_metrics::connect_total_ms.load(std::memory_order_relaxed);
    metrics.tls_total_ms = upstream_metrics::tls_total_ms.load(std::memory_order_relaxed);
    metrics.new_connections = upstream_metrics::new_connections.load(std::memory_order_relaxed);
    metrics.reused_connections = upstream_metrics::reused_connections.load(std::memory_order_relaxed);
    metrics.lease_count = OriginLimiter::instance().lease_count();
    metrics.lease_wait_ms = OriginLimiter::instance().lease_wait_ms();
    metrics.active_leases = OriginLimiter::instance().active();
    return metrics;
}
