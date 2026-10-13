#include "upstream_client.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include <atomic>
#include <cassert>
#include <chrono>
#include <iostream>
#include <memory>
#include <string>
#include <thread>
#include <utility>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

namespace {

// Deterministic stale-keep-alive peer for the pooled-socket retry:
//   1. accept connection 1, answer the first request, keep it open;
//   2. the moment the next request arrives on that pooled socket, RST-close
//      it — this lands after cpp-httplib's liveness pre-check, which silently
//      repairs a socket that is already dead;
//   3. accept the retry on connection 2 and answer it.
// SO_RCVTIMEO keeps the helper from hanging the test when the client
// misbehaves.
void recv_until(int fd, std::string &buffer, const char *needle) {
    char chunk[4096];
    while (buffer.find(needle) == std::string::npos) {
        const ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
        if (n <= 0) return;
        buffer.append(chunk, static_cast<size_t>(n));
    }
}

bool send_all(int fd, const std::string &data) {
    size_t sent = 0;
    while (sent < data.size()) {
        const ssize_t n = ::send(fd, data.data() + sent, data.size() - sent, 0);
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

std::string http_ok_response(const std::string &body, const char *content_type) {
    return "HTTP/1.1 200 OK\r\nContent-Type: " + std::string(content_type) +
           "\r\nContent-Length: " + std::to_string(body.size()) +
           "\r\nConnection: keep-alive\r\n\r\n" + body;
}

int make_listener() {
    const int listener = ::socket(AF_INET, SOCK_STREAM, 0);
    assert(listener >= 0);
    const int one = 1;
    ::setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    timeval accept_timeout{5, 0};
    ::setsockopt(listener, SOL_SOCKET, SO_RCVTIMEO, &accept_timeout,
                 sizeof(accept_timeout));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = 0;
    assert(::bind(listener, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) == 0);
    assert(::listen(listener, 8) == 0);
    return listener;
}

int listener_port(int listener) {
    sockaddr_in addr{};
    socklen_t len = sizeof(addr);
    assert(::getsockname(listener, reinterpret_cast<sockaddr *>(&addr), &len) == 0);
    return ntohs(addr.sin_port);
}

void stale_keepalive_peer(int listener, std::string first_response,
                          std::string retry_response,
                          std::atomic<int> *connections) {
    const timeval recv_timeout{5, 0};
    sockaddr_in addr{};
    socklen_t len = sizeof(addr);
    const int first = ::accept(listener, reinterpret_cast<sockaddr *>(&addr), &len);
    if (first < 0) return;
    ::setsockopt(first, SOL_SOCKET, SO_RCVTIMEO, &recv_timeout,
                 sizeof(recv_timeout));
    connections->fetch_add(1, std::memory_order_relaxed);
    std::string request;
    recv_until(first, request, "\r\n\r\n");
    send_all(first, first_response);
    // Wait for the reused socket to carry the next request, then kill it with
    // an RST so the client observes a Read/Write failure with no response.
    char byte = 0;
    if (::recv(first, &byte, 1, 0) > 0) {
        linger reset{1, 0};
        ::setsockopt(first, SOL_SOCKET, SO_LINGER, &reset, sizeof(reset));
    }
    ::close(first);

    const int retry = ::accept(listener, reinterpret_cast<sockaddr *>(&addr), &len);
    if (retry < 0) return;
    ::setsockopt(retry, SOL_SOCKET, SO_RCVTIMEO, &recv_timeout,
                 sizeof(recv_timeout));
    connections->fetch_add(1, std::memory_order_relaxed);
    request.clear();
    recv_until(retry, request, "\r\n\r\n");
    send_all(retry, retry_response);
    ::close(retry);
}

} // namespace

int main() {
    httplib::Server server;
    server.Get("/fast", [](const httplib::Request &, httplib::Response &res) {
        res.set_content("{\"ok\":true}", "application/json");
    });
    server.Get("/slow", [](const httplib::Request &, httplib::Response &res) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        res.set_content("{\"late\":true}", "application/json");
    });
    server.Get("/slow-stream", [](const httplib::Request &,
                                   httplib::Response &res) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1200));
        res.set_content("data: late\n\n", "text/event-stream");
    });
    server.Get("/metadata-stream", [](const httplib::Request &,
                                       httplib::Response &res) {
        res.set_chunked_content_provider(
            "text/event-stream",
            [](size_t, httplib::DataSink &sink) {
                sink.write(": ping\n\n", 8);
                std::this_thread::sleep_for(std::chrono::milliseconds(1200));
                sink.done();
                return true;
            });
    });
    // Streams some data then ends with a clean EOF and NO protocol terminal
    // frame.  With terminal_seen wired, this must classify as truncated.
    server.Get("/truncated-stream", [](const httplib::Request &,
                                        httplib::Response &res) {
        res.set_chunked_content_provider(
            "text/event-stream",
            [](size_t, httplib::DataSink &sink) {
                sink.write("data: {\"text\":\"partial\"}\n\n", 27);
                sink.done();
                return true;
            });
    });
    // Same shape but the provider DOES send a terminal frame before EOF.
    server.Get("/complete-stream", [](const httplib::Request &,
                                       httplib::Response &res) {
        res.set_chunked_content_provider(
            "text/event-stream",
            [](size_t, httplib::DataSink &sink) {
                sink.write("data: {\"text\":\"full\"}\n\n", 23);
                sink.write("data: [DONE]\n\n", 15);
                sink.done();
                return true;
            });
    });

    const int port = server.bind_to_any_port("127.0.0.1");
    assert(port > 0);
    std::thread server_thread([&] { server.listen_after_bind(); });

    UpstreamClient client;
    ForwardOptions opts;
    opts.non_streaming_timeout = 5;
    opts.non_streaming_total_timeout = 5;

    const auto started = std::chrono::steady_clock::now();
    const auto result = client.forward(
        "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
        "/fast", "", "application/json", nullptr, opts);
    const auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - started).count();

    assert(result.success);
    assert(result.status_code == 200);
    assert(result.body == "{\"ok\":true}");
    // The former per-request watchdog slept for 200ms and was joined here.
    // Leave generous scheduler headroom while still catching that regression.
    assert(elapsed_ms < 150);
    assert(result.duration_ms < 150);

    const auto invalid = client.forward(
        "GET", "missing-scheme", "unused", "/fast", "",
        "application/json", nullptr, opts);
    assert(!invalid.success);
    assert(invalid.status_code == 502);
    assert(invalid.failure_kind == UpstreamFailureKind::Configuration);

    ForwardOptions slow_opts;
    slow_opts.non_streaming_timeout = 1;
    const auto slow = client.forward(
        "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
        "/slow", "", "application/json", nullptr, slow_opts);
    assert(!slow.success);
    assert(slow.is_timeout);
    assert(slow.status_code == 504);
    assert(slow.timeout_secs == 1);
    assert(slow.failure_kind == UpstreamFailureKind::Timeout);

    ForwardOptions first_byte_opts;
    first_byte_opts.streaming_first_byte_timeout = 1;
    first_byte_opts.streaming_semantic_timeout = 5;
    first_byte_opts.streaming_idle_timeout = 5;
    const auto first_byte_timeout = client.forward(
        "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
        "/slow-stream", "", "application/json",
        [](const char *, size_t) { return true; }, first_byte_opts);
    assert(first_byte_timeout.is_timeout);
    assert(first_byte_timeout.status_code == 504);
    assert(first_byte_timeout.timeout_secs == 1);

    ForwardOptions semantic_opts;
    semantic_opts.streaming_first_byte_timeout = 5;
    semantic_opts.streaming_semantic_timeout = 1;
    semantic_opts.streaming_idle_timeout = 5;
    semantic_opts.semantic_seen = std::make_shared<std::atomic<bool>>(false);
    const auto semantic_timeout = client.forward(
        "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
        "/metadata-stream", "", "application/json",
        [](const char *, size_t) { return true; }, semantic_opts);
    assert(semantic_timeout.is_timeout);
    assert(semantic_timeout.status_code == 504);
    assert(semantic_timeout.timeout_secs == 1);

    // Truncation regression: a clean EOF before any terminal event must NOT be
    // a full 2xx success.  The caller supplies terminal_seen; here we simulate
    // a caller whose parser saw no terminal frame.
    {
        ForwardOptions trunc_opts;
        trunc_opts.streaming_first_byte_timeout = 5;
        trunc_opts.streaming_semantic_timeout = 5;
        trunc_opts.streaming_idle_timeout = 5;
        auto terminal_seen = std::make_shared<std::atomic<bool>>(false);
        trunc_opts.terminal_seen = terminal_seen;
        const auto trunc = client.forward(
            "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
            "/truncated-stream", "", "application/json",
            [&terminal_seen](const char *, size_t) {
                return true;  // caller parser never publishes a terminal
            }, trunc_opts);
        assert(!trunc.success);
        assert(trunc.status_code == 200);
        assert(trunc.error.find("truncated") != std::string::npos);
        assert(trunc.failure_kind == UpstreamFailureKind::StreamTruncated);
    }
    // Positive control: a terminal frame published before EOF stays a success.
    {
        ForwardOptions comp_opts;
        comp_opts.streaming_first_byte_timeout = 5;
        comp_opts.streaming_semantic_timeout = 5;
        comp_opts.streaming_idle_timeout = 5;
        auto terminal_seen = std::make_shared<std::atomic<bool>>(false);
        comp_opts.terminal_seen = terminal_seen;
        const auto comp = client.forward(
            "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
            "/complete-stream", "", "application/json",
            [&terminal_seen](const char *data, size_t len) {
                if (std::string(data, len).find("[DONE]") != std::string::npos)
                    terminal_seen->store(true, std::memory_order_release);
                return true;
            }, comp_opts);
        assert(comp.success);
        assert(comp.status_code == 200);
    }

    // ── Stale keep-alive sockets ──────────────────────────────────────────
    // A provider that closed an idle pooled connection used to leak a 502:
    // Error::Read/Write on a reused socket was not classified as retryable.
    // The mock RST-closes the pooled socket only after the next request
    // arrives, so the failure lands inside send/read instead of being
    // repaired by httplib's liveness pre-check.
    {
        const int listener = make_listener();
        std::atomic<int> connections{0};
        std::thread peer(stale_keepalive_peer, listener,
                         http_ok_response("{\"first\":true}", "application/json"),
                         http_ok_response("{\"retry\":true}", "application/json"),
                         &connections);
        const std::string origin = "http://127.0.0.1:" +
                                   std::to_string(listener_port(listener));
        const auto first = client.forward("GET", origin, "unused", "/stale", "",
                                          "application/json", nullptr, opts);
        assert(first.success && first.status_code == 200);
        assert(first.body == "{\"first\":true}");
        const auto second = client.forward("GET", origin, "unused", "/stale", "",
                                           "application/json", nullptr, opts);
        assert(second.success);
        assert(second.status_code == 200);
        assert(second.body == "{\"retry\":true}");
        peer.join();
        ::close(listener);
        assert(connections.load() == 2);
    }
    {
        const int listener = make_listener();
        std::atomic<int> connections{0};
        const std::string sse_first =
            "data: {\"text\":\"first\"}\n\ndata: [DONE]\n\n";
        const std::string sse_retry =
            "data: {\"text\":\"retry\"}\n\ndata: [DONE]\n\n";
        std::thread peer(stale_keepalive_peer, listener,
                         http_ok_response(sse_first, "text/event-stream"),
                         http_ok_response(sse_retry, "text/event-stream"),
                         &connections);
        const std::string origin = "http://127.0.0.1:" +
                                   std::to_string(listener_port(listener));
        auto stream_once = [&] {
            ForwardOptions stream_opts;
            stream_opts.streaming_first_byte_timeout = 5;
            stream_opts.streaming_semantic_timeout = 5;
            stream_opts.streaming_idle_timeout = 5;
            auto terminal_seen = std::make_shared<std::atomic<bool>>(false);
            stream_opts.terminal_seen = terminal_seen;
            std::string forwarded;
            auto result = client.forward(
                "GET", origin, "unused", "/stale", "", "application/json",
                [&forwarded, &terminal_seen](const char *data, size_t len) {
                    forwarded.append(data, len);
                    if (forwarded.find("[DONE]") != std::string::npos)
                        terminal_seen->store(true, std::memory_order_release);
                    return true;
                },
                stream_opts);
            return std::make_pair(result, forwarded);
        };
        const auto first = stream_once();
        assert(first.first.success && first.first.status_code == 200);
        assert(first.second == sse_first);
        const auto second = stream_once();
        assert(second.first.success && second.first.status_code == 200);
        assert(second.second == sse_retry);
        peer.join();
        ::close(listener);
        assert(connections.load() == 2);
    }

    server.stop();
    server_thread.join();
    std::cout << "upstream client tests passed\n";
}
