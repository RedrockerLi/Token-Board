#include "transport_internal.h"

UpstreamClient::ForwardResult
UpstreamClient::forward(const std::string &method,
                        const std::string &base_url,
                        const std::string &upstream_key,
                        const std::string &path,
                        const std::string &body,
                        const std::string &content_type,
                        std::function<bool(const char *, size_t)> on_chunk,
                        const ForwardOptions &opts) {
    ForwardResult result;
    auto t0 = std::chrono::steady_clock::now();

    std::string scheme_host;   // "https://uni-api.cstcloud.cn"
    std::string url_path;      // "/v1"

    size_t scheme_end = base_url.find("://");
    if (scheme_end == std::string::npos) {
        result.status_code = 502;
        result.error = "Invalid base_url: no scheme";
        return result;
    }
    scheme_end += 3;  // past "://"
    if (scheme_end >= base_url.size()) {
        result.status_code = 502;
        result.error = "Invalid base_url: missing host";
        return result;
    }

    size_t path_start = base_url.find('/', scheme_end);
    if (path_start == std::string::npos) {
        scheme_host = base_url;
        url_path = "";
    } else {
        scheme_host = base_url.substr(0, path_start);
        url_path = base_url.substr(path_start);
    }

    std::string full_path = opts.path_is_full ? path : url_path + path;
    const bool streaming = static_cast<bool>(on_chunk);
    const long long deadline_started_ms = now_ms();
    const TimeoutChoice connection_timeout =
        connection_timeout_for(streaming, opts);

    const OriginParts origin_parts = parse_origin(scheme_host);
    if (!origin_parts.valid) {
        result.status_code = 502;
        result.error = "Invalid upstream origin";
        return result;
    }
    const int64_t configured_lease_budget =
        opts.attempt_budget_ms > 0 ? opts.attempt_budget_ms :
        static_cast<int64_t>(std::max(opts.non_streaming_total_timeout,
                                     opts.streaming_first_byte_timeout)) * 1000;
    const int lease_budget_ms = static_cast<int>(
        std::clamp<int64_t>(configured_lease_budget, 1, 5000));
    auto origin_lease = OriginLimiter::instance().acquire(
        origin_parts.origin, lease_budget_ms);
    if (!origin_lease) {
        result.status_code = 503;
        result.error = "Upstream connection lease budget exhausted";
        return result;
    }

    auto watch = std::make_shared<ForwardWatch>();
    watch->semantic_seen = opts.semantic_seen;
    watch->semantic_progress = opts.semantic_progress;
    watch->terminal_seen = opts.terminal_seen;
    watch->downstream_socket = opts.downstream_socket;

    std::vector<std::string> dns_addresses;
    const auto dns_started = std::chrono::steady_clock::now();
    if (numeric_host(origin_parts.hostname)) {
        dns_addresses.push_back(origin_parts.hostname);
    } else {
        const int64_t dns_deadline_ms = deadline_after(
            deadline_started_ms, connection_timeout.seconds * 1000LL);
        auto dns = DnsResolver::instance().resolve(
            origin_parts.hostname, dns_deadline_ms,
            [&watch] {
                return !watch->running.load(std::memory_order_acquire) ||
                       watch->expired.load(std::memory_order_acquire);
            });
        switch (dns.status) {
            case DnsResolution::Status::Ok:
                dns_addresses = std::move(dns.addresses);
                break;
            case DnsResolution::Status::TimedOut:
                result.status_code = 504;
                result.is_timeout = true;
                result.timeout_secs = connection_timeout.seconds;
                result.error = "Upstream DNS deadline exceeded";
                return result;
            default:
                result.status_code = 502;
                result.error = dns.error.empty() ? "Upstream DNS lookup failed"
                                                 : dns.error;
                return result;
        }
    }
    if (dns_addresses.empty()) {
        result.status_code = 502;
        result.error = "Upstream DNS returned no usable addresses";
        return result;
    }
    const int dns_ms = static_cast<int>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - dns_started).count());
    transport_dns_lookups.fetch_add(1, std::memory_order_relaxed);
    transport_dns_total_ms.fetch_add(static_cast<std::uint64_t>(
        std::max(0, dns_ms)), std::memory_order_relaxed);

    httplib::Headers headers;
    if (opts.auth_scheme == "x-api-key") {
        headers = {
            {"x-api-key", upstream_key},
            {"anthropic-version", "2023-06-01"},
            {"Content-Type", content_type},
        };
    } else {
        headers = {
            {"Authorization", "Bearer " + upstream_key},
            {"Content-Type", content_type},
        };
    }

    if (streaming) {
        watch->set_initial_deadlines(deadline_started_ms,
                                     opts.streaming_first_byte_timeout,
                                     opts.streaming_semantic_timeout,
                                     opts.streaming_idle_timeout);
    } else {
        watch->set_initial_deadlines(deadline_started_ms,
                                     opts.non_streaming_total_timeout, 0, 0);
    }
    const bool need_watchdog = streaming
        ? (opts.streaming_first_byte_timeout > 0 ||
           opts.streaming_semantic_timeout > 0 ||
           opts.streaming_idle_timeout > 0)
        : opts.non_streaming_total_timeout > 0;
    const int backstop_sec = streaming
        ? (need_watchdog
               ? std::max({3600, opts.streaming_first_byte_timeout,
                           opts.streaming_semantic_timeout,
                           opts.streaming_idle_timeout})
               : NO_TIMEOUT_SECS)
        : (opts.non_streaming_timeout > 0 ? opts.non_streaming_timeout
                                          : NO_TIMEOUT_SECS);
    if (need_watchdog || opts.downstream_socket >= 0)
        ForwardWatchdog::instance().add(watch);

    struct TimingSample {
        int connect_ms = 0;
        int tls_ms = 0;
    };
    auto timing = std::make_shared<TimingSample>();

    auto configure_client = [&](httplib::Client *cli) {
        cli->set_connection_timeout(connection_timeout.seconds, 0);
        cli->set_write_timeout(30, 0);
        cli->enable_server_certificate_verification(true);
        cli->set_read_timeout(backstop_sec, 0);
        cli->set_socket_options([watch](::socket_t sock) {
            watch->install_socket(sock);
        });
        cli->set_timing_callback([timing](int phase, int elapsed_ms) {
            if (phase == 1) timing->connect_ms = elapsed_ms;
            else if (phase == 2) timing->tls_ms = elapsed_ms;
        });
        watch->attach_client(cli);
    };

    auto is_connect_error = [](httplib::Error e) {
        return e == httplib::Error::Connection ||
               e == httplib::Error::SSLConnection ||
               e == httplib::Error::ConnectionTimeout;
    };

    auto attempt = [&](httplib::Client &cli) -> bool {
        result = ForwardResult{};
        timing->connect_ms = 0;
        timing->tls_ms = 0;
        if (streaming) {
        BoundedTailBuffer accumulated(opts.streaming_body_buffer_limit);
        SseProgressFallback progress_fallback;
        bool client_connected = true;
        bool first_chunk = true;
        std::chrono::steady_clock::time_point t_first;
        std::chrono::steady_clock::time_point request_sent;

        int upstream_status = 0;

        // ContentReceiverWithProgress: (data, len, offset, total) -> bool
        auto receiver = [&](const char *data, size_t len,
                            uint64_t /*offset*/, uint64_t /*total*/) -> bool {
            const auto received_at = std::chrono::steady_clock::now();
            const long long received_ms = now_ms();
            const bool fallback_progress = opts.semantic_progress
                ? false : progress_fallback.feed(data, len);
            if (!watch->begin_chunk(received_ms, fallback_progress))
                return false;
            if (first_chunk) {
                t_first = received_at;
                first_chunk = false;
            }
            const bool successful_status =
                upstream_status >= 200 && upstream_status < 300;
            if (!successful_status) accumulated.append(data, len);
            if (client_connected && successful_status) {
                client_connected = on_chunk(data, len);
            }
            watch->end_chunk(now_ms(), fallback_progress);
            return client_connected;
        };

        httplib::Request req;
        req.method = method;
        req.path = full_path;
        req.headers = headers;
        req.body = body;
        req.response_handler = [&](const httplib::Response &r) -> bool {
            upstream_status = r.status;
            return true;  // always read the body; the receiver decides
        };
        req.content_receiver = receiver;

        httplib::Response upstream_res;
        httplib::Error err;
        request_sent = std::chrono::steady_clock::now();
        bool ok = cli.send(req, upstream_res, err);

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        if (!first_chunk) {
            result.first_byte_ms = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(t_first - request_sent).count());
            result.ttft_ms = result.first_byte_ms;
        } else {
            result.first_byte_ms = result.duration_ms;
            result.ttft_ms = result.duration_ms;  // no chunks received
        }
        result.body_truncated = accumulated.truncated();
        result.body = accumulated.take();

        if (watch->client_disconnected.load(std::memory_order_acquire)) {
            result.status_code = 499;
            result.client_disconnected = true;
            result.success = false;
            result.error = "Client disconnected";
        } else if (ok && !watch->expired.load(std::memory_order_acquire)) {
            result.status_code = upstream_res.status;
            const bool truncated = opts.terminal_seen &&
                !opts.terminal_seen->load(std::memory_order_acquire);
            result.success = (upstream_res.status >= 200 &&
                              upstream_res.status < 300) && !truncated;
            if (!result.success)
                result.error = truncated
                    ? "Upstream stream truncated before terminal event"
                    : "Upstream returned " + std::to_string(upstream_res.status);
            if (upstream_res.status >= 400)
                result.usage_limit = is_usage_limit_error(result.body);
        } else {
            result.is_timeout = watch->expired.load(std::memory_order_acquire) ||
                                err == httplib::Error::ConnectionTimeout;
            if (result.is_timeout) {
                switch (watch->expired_reason.load(std::memory_order_acquire)) {
                    case 1:
                        result.timeout_secs = opts.streaming_first_byte_timeout;
                        break;
                    case 2:
                        result.timeout_secs = opts.streaming_semantic_timeout;
                        break;
                    case 3:
                        result.timeout_secs = opts.streaming_idle_timeout;
                        break;
                    default:
                        result.timeout_secs = connection_timeout.seconds;
                        break;
                }
            }
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            const int reason =
                watch->expired_reason.load(std::memory_order_acquire);
            if (reason == 1)
                result.error = "Upstream first-byte deadline exceeded";
            else if (reason == 2)
                result.error = "Upstream first-semantic deadline exceeded";
            else if (reason == 3)
                result.error =
                    "Upstream semantic-progress idle deadline exceeded";
            else if (err == httplib::Error::ConnectionTimeout)
                result.error = "Upstream connection deadline exceeded";
            else
                result.error = "Upstream request failed: " +
                               std::string(httplib::to_string(err));
            return !ok && !watch->expired.load(std::memory_order_acquire) &&
                   !watch->client_disconnected.load(std::memory_order_acquire) &&
                   is_connect_error(err);
        }
    } else {
        int upstream_status = 0;
        bool response_too_large = false;
        std::string response_body;
        std::chrono::steady_clock::time_point request_sent;
        response_body.reserve(std::min<std::size_t>(body.size() * 2,
                                                     opts.non_streaming_body_limit));
        httplib::Request req;
        req.method = method;
        req.path = full_path;
        req.headers = headers;
        req.body = body;
        req.response_handler = [&](const httplib::Response &response) {
            upstream_status = response.status;
            result.first_byte_ms = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - request_sent).count());
            return true;
        };
        req.content_receiver = [&](const char *data, size_t len,
                                    uint64_t, uint64_t) {
            if (opts.non_streaming_body_limit > 0 &&
                response_body.size() + len > opts.non_streaming_body_limit) {
                response_too_large = true;
                return false;
            }
            response_body.append(data, len);
            return true;
        };
        httplib::Response upstream_res;
        httplib::Error request_error;
        request_sent = std::chrono::steady_clock::now();
        const bool ok = cli.send(req, upstream_res, request_error);

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        if (result.first_byte_ms == 0 && ok)
            result.first_byte_ms = result.duration_ms;
        result.ttft_ms = result.duration_ms;  // non-streaming: no first-token concept

        if (watch->client_disconnected.load(std::memory_order_acquire)) {
            result.status_code = 499;
            result.client_disconnected = true;
            result.success = false;
            result.error = "Client disconnected";
        } else if (ok && !watch->expired.load(std::memory_order_acquire)) {
            result.status_code = upstream_status != 0
                ? upstream_status : upstream_res.status;
            result.body = std::move(response_body);
            result.success = (result.status_code >= 200 && result.status_code < 300);
            if (result.status_code >= 400)
                result.usage_limit = is_usage_limit_error(result.body);
            if (response_too_large) {
                result.body_too_large = true;
                result.success = false;
                result.body.clear();
                result.error = "Upstream response exceeded the non-streaming body limit";
            } else if (!result.success) {
                result.error = "Upstream returned " + std::to_string(result.status_code);
            }
        } else {
            const auto request_err = request_error;
            const bool idle_read_timeout =
                request_err == httplib::Error::Read &&
                opts.non_streaming_timeout > 0 &&
                result.duration_ms + 100 >= opts.non_streaming_timeout * 1000;
            result.is_timeout = watch->expired.load(std::memory_order_acquire) ||
                                request_err == httplib::Error::ConnectionTimeout ||
                                idle_read_timeout;
            if (watch->expired.load(std::memory_order_acquire))
                result.timeout_secs = opts.non_streaming_total_timeout;
            else if (request_err == httplib::Error::ConnectionTimeout)
                result.timeout_secs = connection_timeout.seconds;
            else if (idle_read_timeout)
                result.timeout_secs = opts.non_streaming_timeout;
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            if (watch->expired.load(std::memory_order_acquire))
                result.error = "Upstream total deadline exceeded";
            else if (request_err == httplib::Error::ConnectionTimeout)
                result.error = "Upstream connection deadline exceeded";
            else if (idle_read_timeout)
                result.error = "Upstream read-idle deadline exceeded";
            else
                result.error = "Upstream request failed: " +
                    std::string(httplib::to_string(request_err));
            return !ok &&
                   !watch->expired.load(std::memory_order_acquire) &&
                   !watch->client_disconnected.load(std::memory_order_acquire) &&
                   is_connect_error(request_err);
        }
    }
        return false;
    };
    std::set<std::string> tried_addresses;
    size_t addr_index = 0;
    bool connection_reused = false;
    auto acquire = [&]() -> PooledLease {
        if (auto pooled = ClientPool::instance().take(origin_parts.origin)) {
            connection_reused = true;
            return PooledLease(std::move(*pooled));
        }
        connection_reused = false;
        for (size_t i = addr_index; i < dns_addresses.size(); ++i) {
            if (tried_addresses.count(dns_addresses[i])) continue;
            std::string error;
            auto made = make_client(origin_parts, dns_addresses[i], error);
            if (made) {
                ClientPool::instance().note_created();
                return PooledLease(std::move(*made));
            }
        }
        return PooledLease(std::nullopt);
    };

    PooledLease lease(std::nullopt);
    for (;;) {
        if (!lease.valid()) {
            lease = acquire();
            if (!lease.valid()) {
                result.status_code = 502;
                result.error = "Unable to establish upstream client";
                return result;
            }
            configure_client(lease.client());
        }
        const bool retry = attempt(*lease.client());
        result.dns_ms = dns_ms;
        result.connect_ms = timing->connect_ms;
        result.tls_ms = timing->tls_ms;
        result.lease_wait_ms = origin_lease.wait_ms();
        result.connection_reused = connection_reused;
        transport_connect_total_ms.fetch_add(
            static_cast<std::uint64_t>(std::max(0, result.connect_ms)),
            std::memory_order_relaxed);
        transport_tls_total_ms.fetch_add(
            static_cast<std::uint64_t>(std::max(0, result.tls_ms)),
            std::memory_order_relaxed);
        if (connection_reused)
            transport_reused_connections.fetch_add(1, std::memory_order_relaxed);
        else
            transport_new_connections.fetch_add(1, std::memory_order_relaxed);
        if (retry && addr_index < dns_addresses.size()) {
            const std::string &dead = lease.address();
            tried_addresses.insert(dead);
            DnsResolver::instance().mark_failed(origin_parts.hostname, dead);
            ClientPool::instance().invalidate(origin_parts.origin, dead);
            watch->attach_client(nullptr);
            lease.discard();
            while (addr_index < dns_addresses.size() &&
                   tried_addresses.count(dns_addresses[addr_index]))
                ++addr_index;
            if (addr_index < dns_addresses.size()) continue;
        }
        break;
    }

    watch->finish();
    ForwardWatchdog::instance().changed();

    if (result.success && !numeric_host(origin_parts.hostname) && lease.valid())
        DnsResolver::instance().mark_success(origin_parts.hostname,
                                             lease.address());

    return result;
}

UpstreamClient::TransportMetrics UpstreamClient::transport_metrics() {
    auto metrics = ClientPool::instance().metrics();
    metrics.dns_lookups =
        transport_dns_lookups.load(std::memory_order_relaxed);
    metrics.dns_total_ms =
        transport_dns_total_ms.load(std::memory_order_relaxed);
    metrics.connect_total_ms =
        transport_connect_total_ms.load(std::memory_order_relaxed);
    metrics.tls_total_ms =
        transport_tls_total_ms.load(std::memory_order_relaxed);
    metrics.new_connections =
        transport_new_connections.load(std::memory_order_relaxed);
    metrics.reused_connections =
        transport_reused_connections.load(std::memory_order_relaxed);
    metrics.lease_count = OriginLimiter::instance().lease_count();
    metrics.lease_wait_ms = OriginLimiter::instance().lease_wait_ms();
    metrics.active_leases = OriginLimiter::instance().active();
    return metrics;
}

void UpstreamClient::invalidate_connections() {
    ClientPool::instance().clear();
}

void UpstreamClient::invalidate_connections(
    const std::unordered_set<std::string> &origins) {
    ClientPool::instance().invalidate_origins(origins);
}
