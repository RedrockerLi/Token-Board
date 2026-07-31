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
        std::string error;
        int duration_ms = 0;  // total upstream call time (streaming: until stream ends)
        int ttft_ms = 0;      // time-to-first-token (streaming: first chunk; non-streaming: =duration_ms)
        int retries = 0;      // number of retries performed (0 = first attempt succeeded)
    };

    /// Returns true if the status code is safe to retry.
    /// 4xx errors are client mistakes — retrying won't help.
    /// 5xx / network errors / timeouts are candidates for retry.
    static bool is_retryable(int status_code);

    /// Forward a request to the upstream API.
    ///
    /// If `on_chunk` is provided each chunk of the response body is passed
    /// to the callback as it arrives (SSE streaming).  The full body is
    /// still accumulated and returned in `ForwardResult::body` for later
    /// usage parsing.
    ForwardResult forward(const std::string &method,
                          const std::string &base_url,
                          const std::string &upstream_key,
                          const std::string &path,
                          const std::string &body,
                          const std::string &content_type,
                          std::function<bool(const char *, size_t)> on_chunk,
                          const ForwardOptions &opts = ForwardOptions{});
};
