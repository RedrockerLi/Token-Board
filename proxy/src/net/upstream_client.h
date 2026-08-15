#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <unordered_set>

/// Auth scheme + path handling for an upstream forward call.
struct ForwardOptions {
    std::string auth_scheme = "bearer";  // "bearer" → Authorization: Bearer <key>
                                         // "x-api-key" → x-api-key: <key> + anthropic-version
    // Allowlisted Anthropic beta features from the client request.  The
    // proxy rebuilds upstream headers, so this must be carried explicitly.
    std::string anthropic_beta;
    bool path_is_full = false;           // true → use `path` verbatim (skip base_url path prepend)

    // Upstream timeouts, in seconds (0 = disabled).
    // Streaming: the first chunk and first semantic event have independent
    // deadlines.  After the first semantic event, streaming_idle_timeout is a
    // semantic-progress deadline: transport heartbeats do not extend it.
    // Non-streaming: body reads bounded by non_streaming_timeout (idle semantics).
    int streaming_first_byte_timeout = 60;
    int streaming_semantic_timeout = 60;
    int streaming_idle_timeout = 120;
    int non_streaming_timeout = 600;
    int non_streaming_total_timeout = 600;

    // Exact caller-owned pre-response budget.  When positive, DNS, connect,
    // request write, first byte / first semantic event and the non-streaming
    // total deadline all share this one millisecond budget.  A streaming call
    // that has published its first semantic event leaves this pre-response
    // budget and is subsequently governed only by streaming_idle_timeout.
    int64_t attempt_budget_ms = 0;

    // Maximum retained tail of a streaming response.  Chunks are still
    // forwarded in full; bounding this buffer prevents an arbitrarily long
    // stream from growing proxy memory without limit.  0 disables the bound.
    size_t streaming_body_buffer_limit = 256 * 1024;

    // Non-streaming responses are rejected once they exceed this hard limit;
    // unlike a streaming tail, silently truncating a JSON response is unsafe.
    // 0 disables the limit.
    size_t non_streaming_body_limit = 64 * 1024 * 1024;

    // Shared with the stream parser so the central watchdog observes the first
    // semantic event immediately, before a potentially slow downstream write.
    std::shared_ptr<std::atomic<bool>> semantic_seen;

    // Monotonically incremented by the stream parser for every semantic event
    // (text/reasoning/tool delta).  The watchdog uses changes in this counter,
    // rather than raw SSE chunks, to refresh the post-TTFT idle deadline.
    // Callers which cannot provide it retain a conservative SSE fallback.
    std::shared_ptr<std::atomic<uint64_t>> semantic_progress;

    // Set by the protocol parser only after a complete terminal event has been
    // processed and written downstream.  Returning false from on_chunk with
    // this flag set turns httplib::Error::Canceled into a successful 2xx end;
    // ordinary parser/sink cancellation remains an error.  When supplied, a
    // clean EOF before this flag is set is classified as a truncated stream.
    std::shared_ptr<std::atomic<bool>> terminal_seen;

    // Client socket owned by cpp-httplib's server.  The central watchdog polls
    // it together with all other active forwards and cancels the upstream when
    // the caller disconnects.  -1 disables downstream monitoring.
    int downstream_socket = -1;
};

/// Forwards requests to the upstream CSTCloud API.
///
/// Uses httplib::Client internally.  Supports both regular (full-response)
/// and streaming (chunk-by-chunk callback) forwarding.
class UpstreamClient {
public:
    struct TransportMetrics {
        std::uint64_t pool_hits = 0;
        std::uint64_t pool_misses = 0;
        std::uint64_t clients_created = 0;
        std::uint64_t dns_lookups = 0;
        std::uint64_t dns_total_ms = 0;
        std::uint64_t connect_total_ms = 0;
        std::uint64_t tls_total_ms = 0;
        std::uint64_t new_connections = 0;
        std::uint64_t reused_connections = 0;
        std::uint64_t lease_count = 0;
        std::uint64_t lease_wait_ms = 0;
        std::uint64_t active_leases = 0;
    };
    struct ForwardResult {
        int status_code = 0;
        std::string body;    // full response body (for non-streaming)
        bool success = false;
        bool is_timeout = false;  // true: upstream read timed out / stream interrupted
        bool client_disconnected = false;
        int timeout_secs = 0;     // the timeout value (s) that fired, when is_timeout
        std::string error;
        bool body_truncated = false;  // streaming body contains only its bounded tail
        bool body_too_large = false;  // non-streaming hard response limit fired
        int duration_ms = 0;  // total upstream call time (streaming: until stream ends)
        int dns_ms = 0;
        int connect_ms = 0;
        int tls_ms = 0;
        int lease_wait_ms = 0;
        int first_byte_ms = 0;
        bool connection_reused = false;
        // Streaming: first received body chunk (transport-level precursor to
        // semantic TTFT). Non-streaming: full response duration; it is never
        // displayed or persisted as user-visible TTFT.
        int ttft_ms = 0;
        // Set when a non-2xx error body identifies a genuine quota-exhaustion
        // (opencode.ai "Console Go" sends {"type":"error",
        // "error":{"type":"GoUsageLimitError",...},"metadata":{"limitName":…}}).
        // The caller uses it to distinguish "subscription exhausted — cool this
        // key down" from a transient 429 (rate limit), which only backs off.
        bool usage_limit = false;
    };

    /// Forward a request to the upstream API.
    ///
    /// If `on_chunk` is provided each chunk is passed to the callback as it
    /// arrives. Successful streams retain no body; only an error/truncated
    /// stream keeps a bounded diagnostic tail in `ForwardResult::body`.
    ///
    ForwardResult forward(const std::string &method,
                          const std::string &base_url,
                          const std::string &upstream_key,
                          const std::string &path,
                          const std::string &body,
                          const std::string &content_type,
                          std::function<bool(const char *, size_t)> on_chunk,
                          const ForwardOptions &opts = ForwardOptions{});

    static TransportMetrics transport_metrics();
    // Drop idle keep-alive connections after a routing/origin snapshot
    // replacement. Leased connections finish their current request and are
    // discarded by their normal lease path.
    static void invalidate_connections();
    static void invalidate_connections(
        const std::unordered_set<std::string> &origins);
};
