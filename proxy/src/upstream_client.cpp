#include "upstream_client.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <thread>
#ifdef _WIN32
#include <winsock2.h>
#else
#include <sys/socket.h>
#endif

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"

namespace {

/// A positive select() read-timeout value representing "no timeout".  httplib
/// interprets a 0 read timeout as a non-blocking poll (select with {0,0}),
/// which would fail every read immediately, so "disabled" uses a day.
constexpr int NO_TIMEOUT_SECS = 24 * 3600;

long long now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch()).count();
}

/// Deadline tracker for phase-aware streaming timeouts.  The httplib client's
/// read timeout is select-based and fixed for the whole connection, so it
/// cannot express "first-byte timeout, then idle timeout".  A watchdog thread
/// shutdown()s the upstream socket at each deadline — the same trick the
/// client-disconnect monitor uses — which unblocks the in-flight read and
/// surfaces as a read error (is_timeout).
struct StreamTimeoutWatch {
    std::atomic<bool> running{true};
    std::atomic<bool> got_first{false};
    std::atomic<long long> first_byte_deadline_ms{0};  // 0 = disabled
    std::atomic<long long> idle_deadline_ms{0};        // 0 = no idle deadline
};

std::thread spawn_timeout_watchdog(StreamTimeoutWatch &w,
                                   const std::atomic<int> &fd) {
    return std::thread([&w, &fd] {
        while (w.running.load(std::memory_order_acquire)) {
            long long now = now_ms();
            bool expired = false;
            if (!w.got_first.load(std::memory_order_acquire) &&
                w.first_byte_deadline_ms.load(std::memory_order_relaxed) != 0 &&
                now > w.first_byte_deadline_ms.load(std::memory_order_relaxed))
                expired = true;
            if (w.got_first.load(std::memory_order_acquire) &&
                w.idle_deadline_ms.load(std::memory_order_relaxed) != 0 &&
                now > w.idle_deadline_ms.load(std::memory_order_relaxed))
                expired = true;
            if (expired) {
                int s = fd.load(std::memory_order_relaxed);
                if (s >= 0) ::shutdown(s, SHUT_RDWR);  // unblock the read
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }
    });
}

}  // namespace

UpstreamClient::ForwardResult
UpstreamClient::forward(const std::string &method,
                        const std::string &base_url,
                        const std::string &upstream_key,
                        const std::string &path,
                        const std::string &body,
                        const std::string &content_type,
                        std::function<bool(const char *, size_t)> on_chunk,
                        const ForwardOptions &opts,
                        std::function<void(int)> on_socket) {
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

    // Build the full upstream path.  path_is_full bypasses base_url's path
    // component (used for explicit endpoint_path overrides).
    std::string full_path = opts.path_is_full ? path : url_path + path;

    // Create client
    httplib::Client cli(scheme_host);
    cli.set_connection_timeout(10, 0);   // 10 sec connect timeout
    cli.set_write_timeout(30, 0);
    cli.enable_server_certificate_verification(true);

    // Expose the upstream socket fd so the caller can shutdown() it from a
    // monitor thread to unblock an in-flight read (e.g. client disconnect),
    // and so the streaming timeout watchdog can do the same at its deadlines.
    std::atomic<int> upstream_sock{-1};
    if (on_socket) {
        cli.set_socket_options([on_socket, &upstream_sock](int sock) {
            upstream_sock.store(sock, std::memory_order_relaxed);
            on_socket(sock);
        });
    }

    // Build headers.  Anthropic-native upstreams use x-api-key instead of
    // Authorization: Bearer.
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

    if (on_chunk) {
        // ── Streaming path: use Request::content_receiver ─────────
        std::string accumulated;
        bool client_connected = true;
        bool first_chunk = true;
        std::chrono::steady_clock::time_point t_first;

        // Phase-aware streaming timeouts (see StreamTimeoutWatch): httplib's
        // read timeout is select-based and fixed for the connection, so it
        // cannot express "first-byte timeout, then idle timeout".  A watchdog
        // thread shutdown()s the upstream socket at each deadline; the socket's
        // own select timeout is set to a large backstop so it never fires first.
        StreamTimeoutWatch wd;
        if (opts.streaming_first_byte_timeout > 0)
            wd.first_byte_deadline_ms.store(
                now_ms() +
                    static_cast<long long>(opts.streaming_first_byte_timeout) * 1000,
                std::memory_order_relaxed);
        bool need_watchdog = opts.streaming_first_byte_timeout > 0 ||
                             opts.streaming_idle_timeout > 0;
        int backstop_sec = need_watchdog
            ? std::max({3600, opts.streaming_first_byte_timeout,
                        opts.streaming_idle_timeout})
            : NO_TIMEOUT_SECS;
        cli.set_read_timeout(backstop_sec, 0);
        std::thread watchdog;
        if (need_watchdog) watchdog = spawn_timeout_watchdog(wd, upstream_sock);

        // ContentReceiverWithProgress: (data, len, offset, total) -> bool
        auto receiver = [&](const char *data, size_t len,
                            uint64_t /*offset*/, uint64_t /*total*/) -> bool {
            if (first_chunk) {
                t_first = std::chrono::steady_clock::now();
                first_chunk = false;
                wd.got_first.store(true, std::memory_order_release);
            }
            // Reset the idle deadline on every chunk (first one included).
            if (opts.streaming_idle_timeout > 0)
                wd.idle_deadline_ms.store(
                    now_ms() +
                        static_cast<long long>(opts.streaming_idle_timeout) * 1000,
                    std::memory_order_release);
            accumulated.append(data, len);
            if (client_connected) {
                client_connected = on_chunk(data, len);
            }
            return client_connected;
        };

        httplib::Request req;
        req.method = method;
        req.path = full_path;
        req.headers = headers;
        req.body = body;
        req.content_receiver = receiver;

        httplib::Response upstream_res;
        httplib::Error err;
        bool ok = cli.send(req, upstream_res, err);

        wd.running.store(false, std::memory_order_release);
        if (watchdog.joinable()) watchdog.join();

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
            result.is_timeout = (err == httplib::Error::ConnectionTimeout ||
                                 err == httplib::Error::Read);
            result.timeout_secs = result.is_timeout
                ? (first_chunk ? opts.streaming_first_byte_timeout
                               : opts.streaming_idle_timeout)
                : 0;
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            result.error = "Upstream request failed: " +
                           std::string(httplib::to_string(err));
        }
    } else {
        // ── Non-streaming: use GET or POST based on method ─────
        // Bounded by non_streaming_timeout (idle semantics: a read that gets no
        // data for N seconds fails).  NO_TIMEOUT_SECS when disabled (a 0 read
        // timeout would be a non-blocking poll, failing every read instantly).
        cli.set_read_timeout(opts.non_streaming_timeout > 0
                                 ? opts.non_streaming_timeout : NO_TIMEOUT_SECS, 0);
        httplib::Result upstream_res;
        if (method == "GET") {
            upstream_res = cli.Get(full_path, headers);
        } else {
            upstream_res = cli.Post(full_path, headers, body, content_type.c_str());
        }

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
            result.is_timeout = (upstream_res.error() == httplib::Error::ConnectionTimeout ||
                                 upstream_res.error() == httplib::Error::Read);
            result.timeout_secs = result.is_timeout ? opts.non_streaming_timeout : 0;
            result.status_code = result.is_timeout ? 504 : 502;
            result.success = false;
            result.error = "Upstream request failed: " +
                           std::string(httplib::to_string(upstream_res.error()));
        }
    }

    return result;
}
