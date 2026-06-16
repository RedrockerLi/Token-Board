#include "upstream_client.h"

#include <chrono>

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

UpstreamClient::ForwardResult
UpstreamClient::forward(const std::string &base_url,
                        const std::string &upstream_key,
                        const std::string &path,
                        const std::string &body,
                        const std::string &content_type,
                        std::function<bool(const char *, size_t)> on_chunk) {
    ForwardResult result;
    auto t0 = std::chrono::steady_clock::now();

    // Parse base URL into scheme + host components
    // base_url is e.g. "https://uni-api.cstcloud.cn/v1"
    std::string scheme_host;   // "https://uni-api.cstcloud.cn"
    std::string url_path;      // "/v1"

    size_t scheme_end = base_url.find("://");
    if (scheme_end == std::string::npos) {
        result.error = "Invalid base_url: no scheme";
        return result;
    }
    scheme_end += 3;  // past "://"

    size_t path_start = base_url.find('/', scheme_end);
    if (path_start == std::string::npos) {
        scheme_host = base_url;
        url_path = "";
    } else {
        scheme_host = base_url.substr(0, path_start);
        url_path = base_url.substr(path_start);
    }

    // Build the full upstream path
    std::string full_path = url_path + path;

    // Create client
    httplib::Client cli(scheme_host);
    cli.set_connection_timeout(10, 0);   // 10 sec connect timeout
    cli.set_read_timeout(300, 0);        // 5 min read timeout (for long generations)
    cli.set_write_timeout(30, 0);
    cli.enable_server_certificate_verification(true);

    // Build headers
    httplib::Headers headers = {
        {"Authorization", "Bearer " + upstream_key},
        {"Content-Type", content_type},
    };

    if (on_chunk) {
        // ── Streaming path: use Request::content_receiver ─────────
        std::string accumulated;
        bool client_connected = true;
        bool first_chunk = true;
        std::chrono::steady_clock::time_point t_first;

        // ContentReceiverWithProgress: (data, len, offset, total) -> bool
        auto receiver = [&](const char *data, size_t len,
                            uint64_t /*offset*/, uint64_t /*total*/) -> bool {
            if (first_chunk) {
                t_first = std::chrono::steady_clock::now();
                first_chunk = false;
            }
            accumulated.append(data, len);
            if (client_connected) {
                client_connected = on_chunk(data, len);
            }
            return client_connected;
        };

        httplib::Request req;
        req.method = "POST";
        req.path = full_path;
        req.headers = headers;
        req.body = body;
        req.content_receiver = receiver;

        httplib::Response upstream_res;
        httplib::Error err;
        bool ok = cli.send(req, upstream_res, err);

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        if (!first_chunk) {
            result.ttft_ms = static_cast<int>(
                std::chrono::duration_cast<std::chrono::milliseconds>(t_first - t0).count());
        } else {
            result.ttft_ms = result.duration_ms;  // no chunks received
        }
        result.body = std::move(accumulated);

        if (ok) {
            result.status_code = upstream_res.status;
            result.success = (upstream_res.status >= 200 && upstream_res.status < 300);
            if (!result.success)
                result.error = "Upstream returned " + std::to_string(upstream_res.status);
        } else {
            result.status_code = 502;
            result.success = false;
            result.error = "Upstream request failed: " +
                           std::string(httplib::to_string(err));
        }
    } else {
        // ── Non-streaming: simple synchronous POST ─────────────────
        httplib::Result upstream_res =
            cli.Post(full_path, headers, body, content_type.c_str());

        auto t1 = std::chrono::steady_clock::now();
        result.duration_ms = static_cast<int>(
            std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count());
        result.ttft_ms = result.duration_ms;  // non-streaming: no first-token concept

        if (upstream_res) {
            result.status_code = upstream_res->status;
            result.body = upstream_res->body;
            result.success = (upstream_res->status >= 200 && upstream_res->status < 300);
            if (!result.success)
                result.error = "Upstream returned " + std::to_string(upstream_res->status);
        } else {
            result.status_code = 502;
            result.success = false;
            result.error = "Upstream request failed: " +
                           std::string(httplib::to_string(upstream_res.error()));
        }
    }

    return result;
}
