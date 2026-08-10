UpstreamClient::TransportMetrics UpstreamClient::transport_metrics() {
    auto metrics = ClientPool::instance().metrics();
    metrics.dns_lookups = transport_dns_lookups.load(std::memory_order_relaxed);
    metrics.dns_total_ms = transport_dns_total_ms.load(std::memory_order_relaxed);
    metrics.lease_count = OriginLimiter::instance().lease_count();
    metrics.lease_wait_ms = OriginLimiter::instance().lease_wait_ms();
    metrics.active_leases = OriginLimiter::instance().active();
    return metrics;
}
