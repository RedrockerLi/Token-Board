#include "upstream_client.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include <algorithm>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <iostream>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

// Connection-reuse / DNS-cache regression gate for the pooled forward() path.
//
// A fresh httplib::Client per request (pre-pooling) opens one TCP connection
// per request.  Every request on a keep-alive connection shares the same
// source (ephemeral) port, so counting the DISTINCT client ports seen by the
// server equals the number of connections the proxy actually dialed:
//   * pre-wiring:  ~one connection per request (distinct ports ≈ N)
//   * after wiring: one pooled connection reused (distinct ports ≈ 1)
//
// Mode (argv[1]):
//   reuse  (default)  — assert connections stay far below request count.
//   fresh             — assert ~one connection per request (pre-wiring
//                       baseline, used to capture the "Before" number).
//
// Prints the measured connection count + p50/p99 latency for the report.

static std::mutex g_mutex;
static std::set<int> g_ports;

int main(int argc, char **argv) {
    bool expect_reuse = true;
    if (argc > 1 && std::string(argv[1]) == "fresh") expect_reuse = false;

    httplib::Server server;
    server.set_tcp_nodelay(true);  // real upstreams run TCP_NODELAY
    server.set_pre_routing_handler(
        [](const httplib::Request &req, httplib::Response &) {
            if (req.remote_port > 0) {
                std::lock_guard<std::mutex> lock(g_mutex);
                g_ports.insert(req.remote_port);
            }
            return httplib::Server::HandlerResponse::Unhandled;
        });
    server.Get("/fast", [](const httplib::Request &, httplib::Response &res) {
        res.set_content("{\"ok\":true}", "application/json");
    });

    const int port = server.bind_to_any_port("127.0.0.1");
    assert(port > 0);
    std::thread server_thread([&] { server.listen_after_bind(); });
    // Wait until the server is actually accepting.
    httplib::Client probe("http://127.0.0.1:" + std::to_string(port));
    for (int i = 0; i < 100; ++i) {
        if (auto r = probe.Get("/fast")) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    const std::string origin = "http://127.0.0.1:" + std::to_string(port);
    UpstreamClient client;
    ForwardOptions opts;
    opts.non_streaming_timeout = 5;
    opts.non_streaming_total_timeout = 5;

    constexpr int kRequests = 50;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_ports.clear();  // discount the probe / bind-time connections
    }
    std::vector<int> latencies;
    latencies.reserve(kRequests);
    bool all_ok = true;
    for (int i = 0; i < kRequests; ++i) {
        auto start = std::chrono::steady_clock::now();
        const auto r = client.forward("GET", origin, "unused", "/fast", "",
                                      "application/json", nullptr, opts);
        latencies.push_back(static_cast<int>(std::chrono::duration_cast<
            std::chrono::microseconds>(std::chrono::steady_clock::now() - start)
                                                 .count()));
        if (!(r.success && r.status_code == 200 && r.body == "{\"ok\":true}"))
            all_ok = false;
    }
    size_t distinct_connections;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        distinct_connections = g_ports.size();
    }

    // DNS path: "localhost" is a real hostname, so this exercises
    // DnsResolver::resolve + make_client(set_hostname_addr_map) + hostname
    // verification + multi-address iteration (::1 vs 127.0.0.1).
    const auto dns_result = client.forward(
        "GET", "http://localhost:" + std::to_string(port), "unused", "/fast",
        "", "application/json", nullptr, opts);

    // Concurrency sanity: 8 threads × 5 sequential forwards each — must all
    // succeed (the pool hands out distinct clients, one per in-flight lease).
    std::atomic<bool> concurrent_ok{true};
    std::vector<std::thread> workers;
    for (int t = 0; t < 8; ++t) {
        workers.emplace_back([&] {
            UpstreamClient c;
            for (int i = 0; i < 5; ++i) {
                const auto r = c.forward("GET", origin, "unused", "/fast", "",
                                         "application/json", nullptr, opts);
                if (!(r.success && r.status_code == 200)) concurrent_ok = false;
            }
        });
    }
    for (auto &w : workers) w.join();

    std::sort(latencies.begin(), latencies.end());
    const auto p50 = latencies[latencies.size() / 2];
    const auto p99 = latencies[latencies.size() * 99 / 100];
    std::printf("distinct_connections=%zu requests=%d p50=%ldus p99=%ldus\n",
                distinct_connections, kRequests, p50, p99);
    std::printf("dns(localhost) status=%d ok=%d\n", dns_result.status_code,
                dns_result.success);
    std::printf("concurrent(8x5) ok=%d\n", concurrent_ok.load());

    assert(all_ok);
    assert(dns_result.success && dns_result.status_code == 200);
    assert(concurrent_ok.load());
    if (expect_reuse) {
        // Sequential requests share the pooled keep-alive connection.  Allow a
        // small slack for a single reconnect; a pre-wiring build sees ~50.
        assert(distinct_connections <= 3 &&
               "forward() must reuse pooled connections");
    } else {
        // Baseline mode: pre-pooling, every request opens a fresh connection.
        assert(distinct_connections >= kRequests - 2);
    }

    server.stop();
    server_thread.join();
    std::cout << "upstream client perf test "
              << (expect_reuse ? "(reuse gate)" : "(fresh baseline)")
              << " passed\n";
    return 0;
}
