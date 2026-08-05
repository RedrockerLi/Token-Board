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

    ForwardOptions slow_opts;
    slow_opts.non_streaming_timeout = 1;
    const auto slow = client.forward(
        "GET", "http://127.0.0.1:" + std::to_string(port), "unused",
        "/slow", "", "application/json", nullptr, slow_opts);
    assert(!slow.success);
    assert(slow.is_timeout);
    assert(slow.status_code == 504);
    assert(slow.timeout_secs == 1);

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

    server.stop();
    server_thread.join();
    std::cout << "upstream client tests passed\n";
}
