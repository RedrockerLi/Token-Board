#pragma once

#include <functional>
#include <string>

/// Auth scheme + path handling for an upstream forward call.
struct ForwardOptions {
    std::string auth_scheme = "bearer";  // "bearer" → Authorization: Bearer <key>
                                         // "x-api-key" → x-api-key: <key> + anthropic-version
    bool path_is_full = false;           // true → use `path` verbatim (skip base_url path prepend)
};

/// Forwards requests to the upstream CSTCloud API.
///
/// Uses httplib::Client internally.  Supports both regular (full-response)
/// and streaming (chunk-by-chunk callback) forwarding.
class UpstreamClient {
public:
    struct ForwardResult {
        int status_code = 0;
        std::string body;    // full response body (for non-streaming)
        bool success = false;
        bool is_timeout = false;  // true: upstream read timed out / stream interrupted
        std::string error;
        int duration_ms = 0;  // total upstream call time (streaming: until stream ends)
        int ttft_ms = 0;      // time-to-first-token (streaming: first chunk; non-streaming: =duration_ms)
    };

    /// Forward a request to the upstream API.
    ///
    /// If `on_chunk` is provided each chunk of the response body is passed
    /// to the callback as it arrives (SSE streaming).  The full body is
    /// still accumulated and returned in `ForwardResult::body` for later
    /// usage parsing.
    ///
    /// If `on_socket` is provided, it is invoked with the upstream socket fd
    /// once the connection is established — the caller can `shutdown()` it
    /// from another thread to unblock an in-flight read (e.g. on client
    /// disconnect).
    ForwardResult forward(const std::string &method,
                          const std::string &base_url,
                          const std::string &upstream_key,
                          const std::string &path,
                          const std::string &body,
                          const std::string &content_type,
                          std::function<bool(const char *, size_t)> on_chunk,
                          const ForwardOptions &opts = ForwardOptions{},
                          std::function<void(int)> on_socket = {});
};
