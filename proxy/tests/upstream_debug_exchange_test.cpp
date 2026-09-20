#include "upstream_client.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

#include "logging.h"

#include <cassert>
#include <functional>
#include <string>
#include <thread>

#include <unistd.h>

namespace {

std::string capture_stdout(const std::function<void()> &fn) {
    int pipe_fds[2];
    assert(pipe(pipe_fds) == 0);
    const int saved_stdout = dup(STDOUT_FILENO);
    assert(saved_stdout >= 0);
    std::fflush(stdout);
    assert(dup2(pipe_fds[1], STDOUT_FILENO) >= 0);
    close(pipe_fds[1]);

    fn();
    std::fflush(stdout);
    assert(dup2(saved_stdout, STDOUT_FILENO) >= 0);
    close(saved_stdout);

    std::string output;
    char buffer[1024];
    for (;;) {
        const ssize_t count = read(pipe_fds[0], buffer, sizeof(buffer));
        if (count <= 0) break;
        output.append(buffer, static_cast<size_t>(count));
    }
    close(pipe_fds[0]);
    return output;
}

}  // namespace

int main() {
    httplib::Server server;
    server.Post("/echo", [](const httplib::Request &, httplib::Response &res) {
        res.set_content("{\"reply\":\"ok\"}", "application/json");
    });
    server.Get("/stream", [](const httplib::Request &, httplib::Response &res) {
        res.set_chunked_content_provider(
            "text/event-stream", [](size_t, httplib::DataSink &sink) {
                sink.write("chunk-1", 7);
                sink.write("chunk-2", 7);
                sink.done();
                return true;
            });
    });

    const int port = server.bind_to_any_port("127.0.0.1");
    assert(port > 0);
    std::thread server_thread([&] { server.listen_after_bind(); });

    tb_log_level.store(static_cast<int>(LogLevel::Debug),
                       std::memory_order_release);
    UpstreamClient client;
    ForwardOptions options;
    options.non_streaming_timeout = 5;
    options.non_streaming_total_timeout = 5;
    const std::string origin = "http://127.0.0.1:" + std::to_string(port);

    const auto non_stream_output = capture_stdout([&] {
        const auto result = client.forward(
            "POST", origin, "Bearer top-secret", "/echo",
            "{\"prompt\":\"hello\"}", "application/json", nullptr,
            options);
        assert(result.success);
        assert(result.body == "{\"reply\":\"ok\"}");
    });
    assert(non_stream_output.find("[HTTP_DEBUG][upstream] request") !=
           std::string::npos);
    assert(non_stream_output.find("target=\"http://127.0.0.1:") !=
           std::string::npos);
    assert(non_stream_output.find("body=\"{\\\"prompt\\\":\\\"hello\\\"}\"") !=
           std::string::npos);
    assert(non_stream_output.find("Bearer top-secret") == std::string::npos);
    assert(non_stream_output.find("response_headers status=200") !=
           std::string::npos);
    assert(non_stream_output.find("response_body bytes=") != std::string::npos);

    std::string streamed;
    ForwardOptions stream_options;
    stream_options.streaming_first_byte_timeout = 5;
    stream_options.streaming_semantic_timeout = 5;
    stream_options.streaming_idle_timeout = 5;
    const auto stream_output = capture_stdout([&] {
        const auto result = client.forward(
            "GET", origin, "Bearer top-secret", "/stream", "",
            "text/event-stream",
            [&](const char *data, size_t size) {
                streamed.append(data, size);
                return true;
            }, stream_options);
        assert(result.success);
        assert(streamed == "chunk-1chunk-2");
    });
    assert(stream_output.find("response_chunk index=1 bytes=7") !=
           std::string::npos);
    assert(stream_output.find("response_chunk index=2 bytes=7") !=
           std::string::npos);
    assert(stream_output.find("payload=\"chunk-1\"") != std::string::npos);
    assert(stream_output.find("payload=\"chunk-2\"") != std::string::npos);

    server.stop();
    server_thread.join();
    std::puts("upstream debug exchange tests passed");
}
