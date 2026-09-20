#include "http_debug_log.h"

#include <cassert>
#include <cstdio>
#include <string>

#include <unistd.h>

int main() {
    int pipe_fds[2];
    assert(pipe(pipe_fds) == 0);
    const int saved_stdout = dup(STDOUT_FILENO);
    assert(saved_stdout >= 0);
    assert(dup2(pipe_fds[1], STDOUT_FILENO) >= 0);
    close(pipe_fds[1]);

    tb_log_level.store(static_cast<int>(LogLevel::Debug),
                       std::memory_order_release);
    httplib::Headers headers{
        {"Authorization", "Bearer top-secret"},
        {"Content-Type", "application/json"},
    };
    tb_http_debug::request(
        "upstream", "POST", "https://provider.test/v1/chat",
        headers, "{\"message\":\"line 1\\nline 2\"}");
    tb_http_debug::response_headers("upstream", "/v1/chat", 200, headers);
    tb_http_debug::response_body("downstream", "/v1/chat", "{\"ok\":true}");
    tb_http_debug::response_chunk("downstream", "/v1/chat", 1,
                                  "data: done\n\n", 12);
    tb_http_debug::exchange_result("upstream", "/v1/chat", 200, true,
                                   "", 12, 12);
    std::fflush(stdout);

    assert(dup2(saved_stdout, STDOUT_FILENO) >= 0);
    close(saved_stdout);

    std::string output;
    char buffer[512];
    for (;;) {
        const ssize_t count = read(pipe_fds[0], buffer, sizeof(buffer));
        if (count <= 0) break;
        output.append(buffer, static_cast<size_t>(count));
    }
    close(pipe_fds[0]);

    assert(output.find("[HTTP_DEBUG][upstream] request") != std::string::npos);
    assert(output.find("body=\"{\\\"message\\\":\\\"line 1\\\\nline 2\\\"}\"") !=
           std::string::npos);
    assert(output.find("<redacted>") != std::string::npos);
    assert(output.find("Bearer top-secret") == std::string::npos);
    assert(output.find("response_body bytes=11") != std::string::npos);
    assert(output.find("response_chunk index=1 bytes=12") != std::string::npos);
    assert(output.find("exchange_result status=200 transport=ok") !=
           std::string::npos);
    std::puts("http debug log tests passed");
}
