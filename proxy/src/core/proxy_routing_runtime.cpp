#include "proxy_server_internal.h"
#include "usage_parser.h"

std::vector<UpstreamCandidate> ProxyServer::resolve_candidates_cached(
    const Router::RouteResult &route, std::string &model) {
    model = fmt::strip_one_m_suffix_for_upstream(model);
    return resolve_candidates_uncached(router_, route, model);
}

// ── add_cors_headers ─────────────────────────────────────────────────────

void ProxyServer::add_cors_headers(httplib::Response &res) {
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.set_header("Access-Control-Allow-Headers",
                   "Authorization, Content-Type, X-API-Key, X-Session-ID, "
                   "X-Conversation-ID");
}

// ── Format resolution helpers ────────────────────────────────────────────

/// Resolve the harness (client-side) format from the incoming request URL path.
/// Each chat endpoint has a canonical wire format:
///   /v1/chat/completions → OpenAI, /v1/responses → OpenAI Responses,
///   /v1/messages → Anthropic.
///
/// A client whose base URL already ends in `/v1` (e.g.
/// `ANTHROPIC_BASE_URL=http://host:8800/v1`) appends the endpoint again,
/// producing `/v1/v1/messages`.  Tolerate that double `/v1` prefix (mirrors
/// cc-switch, which registers `/v1/v1/chat/completions` etc.).
ir::ApiFormat harness_format_from_path(const std::string &path) {
    return endpoint_policy_for_path(path).client_format;
}

/// Parsed exactly once at the HTTP boundary and shared by routing, validation,
/// conversion, streaming and session affinity.
std::vector<CandidateRequestBody> candidate_request_bodies(
    const std::shared_ptr<const std::string> &original_body,
    const json &parsed,
    const std::string &requested_model,
    const std::vector<UpstreamCandidate> &candidates,
    ir::ApiFormat client_format) {
    std::vector<CandidateRequestBody> bodies;
    bodies.reserve(candidates.size());
    for (const auto &candidate : candidates) {
        // Cross-format attempts build their body lazily from the shared IR in
        // the stream callback.  Do not eagerly serialize an unused OpenAI-
        // shaped body for those candidates.
        if (ir::parse_api_format(candidate.account().api_format) != client_format) {
            bodies.emplace_back();
            continue;
        }
        if (candidate.upstream_model() == requested_model) {
            bodies.push_back(original_body);
            continue;
        }
        // Same-format model rewrites are deliberately lazy. The deferred
        // stream provider may never try this candidate after an earlier
        // healthy key succeeds; serializing here would waste CPU and memory.
        // Keep the slot empty and let StreamPipeline cache the first actual
        // (format,model) attempt. `parsed` is retained in the function
        // signature for compatibility with non-stream callers.
        (void)parsed;
        bodies.emplace_back();
    }
    return bodies;
}

/// Resolved upstream target (path + auth/path options) for a route.
UpstreamTarget resolve_upstream_target(const EndpointPolicy &policy,
                                       const std::string &api_format,
                                       const std::string &base_url,
                                       const std::string &endpoint_path,
                                       const std::string &auth_header,
                                       const Database::TimeoutConfig &tc) {
    UpstreamTarget t;
    resolve_upstream_path(policy, api_format, base_url, endpoint_path, t.path,
                          t.opts.path_is_full);
    t.opts.auth_scheme = resolve_auth_scheme(api_format, auth_header);
    t.opts.streaming_first_byte_timeout = tc.streaming_first_byte_timeout;
    t.opts.streaming_semantic_timeout = tc.streaming_first_byte_timeout;
    t.opts.streaming_idle_timeout = tc.streaming_idle_timeout;
    t.opts.non_streaming_timeout = tc.non_streaming_timeout;
    t.opts.non_streaming_total_timeout = tc.non_streaming_timeout;
    return t;
}

std::string resolve_auth_scheme(const std::string &api_format,
                                const std::string &configured) {
    const auto format = ir::parse_api_format(api_format);
    // Snapshot loading already emits these canonical spellings for normal
    // configuration. Keep the hot path allocation-free for that common case.
    if (configured == "bearer" || configured == "x-api-key") return configured;
    if (configured == "auto")
        return format == ir::ApiFormat::Anthropic ? "x-api-key" : "bearer";
    std::string scheme = configured;
    std::transform(scheme.begin(), scheme.end(), scheme.begin(),
                   [](unsigned char value) {
                       return static_cast<char>(std::tolower(value));
                   });
    if (scheme.empty() || scheme == "auto")
        return format == ir::ApiFormat::Anthropic ? "x-api-key" : "bearer";
    if (scheme == "x-api-key" || scheme == "bearer") return scheme;
    // UpstreamClient supports only these two wire forms. Preserve the
    // historical safe default for malformed configuration rather than
    // accidentally sending an unsupported authentication header.
    return "bearer";
}

/// Per-format timeout config for a client request, mirroring cc-switch's
/// per-app-type timeouts keyed here by the client's wire format.
Database::TimeoutConfig ProxyServer::timeout_config_cached(
    EndpointKind kind) {
    return router_.timeout_config(endpoint_policy(kind).name);
}

std::uint64_t ProxyServer::request_started(const std::string &model,
                                           bool streaming) {
    std::uint64_t id = next_request_id_.fetch_add(1, std::memory_order_relaxed);
    // Zero is the invalid sentinel. The wrap can only occur after 2^64-1
    // requests, but handle it without leaking the live-request entry.
    if (id == 0)
        id = next_request_id_.fetch_add(1, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lock(live_requests_mutex_);
        live_requests_.emplace(id, LiveRequest{
            model, streaming, std::chrono::steady_clock::now()});
    }
    in_flight_count_.fetch_add(1, std::memory_order_relaxed);
    return id;
}

void ProxyServer::request_finished(std::uint64_t request_id) {
    if (request_id == 0) return;
    bool removed = false;
    {
        std::lock_guard<std::mutex> lock(live_requests_mutex_);
        removed = live_requests_.erase(request_id) != 0;
    }
    if (removed) in_flight_count_.fetch_sub(1, std::memory_order_relaxed);
}

/// Retries share one pre-response budget.  Without this clamp, N unhealthy
/// keys each consume the full configured timeout and turn a 60s request into
/// an N×60s stall.  A successfully committed stream is not cut off here; its
/// normal idle timeout continues to protect the established stream.
bool clamp_to_remaining_budget(Database::TimeoutConfig &tc,
                                      std::chrono::steady_clock::time_point deadline,
                                      bool streaming) {
    auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now()).count();
    if (remaining <= 0) return false;
    const int seconds = std::max(1, static_cast<int>((remaining + 999) / 1000));
    int &field = streaming ? tc.streaming_first_byte_timeout
                           : tc.non_streaming_timeout;
    field = field > 0 ? std::min(field, seconds) : seconds;
    return true;
}

/// Non-streaming usage parser dispatcher by upstream api_format.
std::optional<UsageAccounting>
parse_usage_for_format(const std::string &api_format, const std::string &body) {
    const auto format = ir::parse_api_format(api_format);
    const auto wire = fmt::parse_usage_for_format(format, body);
    if (!wire) return std::nullopt;
    return UsageAccounting::from_ir(wire->usage, format, wire->model);
}

/// Project the format/IR usage into the database-compatible accounting shape.
UsageAccounting usage_from_ir(const ir::Usage &u,
                              ir::ApiFormat upstream_fmt) {
    return UsageAccounting::from_ir(u, upstream_fmt);
}

/// Check whether the client disconnected while we waited for upstream.
bool client_disconnected(const httplib::Request &req,
                                std::uint64_t inflight_id,
                                const std::string &model) {
    if (req.client_socket == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = req.client_socket;
    // POLLRDHUP only means that the peer closed its write half. A client may
    // legitimately send one complete request, shutdown(SHUT_WR), and keep
    // reading the response, so only hard socket errors count as disconnects.
    pfd.events = POLLIN;
    pfd.revents = 0;
    if (poll(&pfd, 1, 0) > 0 &&
        (pfd.revents & (POLLHUP | POLLERR | POLLNVAL))) {
        TB_LOG_DEBUG("[Proxy] Client gone, drop response "
                        "(inflight=%llu, model=%s)\n",
                static_cast<unsigned long long>(inflight_id), model.c_str());
        return true;
    }
    return false;
}

/// True if the client socket is already closed.  Used as a final race-safe
/// check after the process-wide watchdog has cancelled an upstream request.
bool client_socket_gone(int sock) {
    if (sock == INVALID_SOCKET) return false;
    struct pollfd pfd;
    pfd.fd = sock;
    pfd.events = POLLIN;
    pfd.revents = 0;
    return poll(&pfd, 1, 0) > 0 &&
           (pfd.revents & (POLLHUP | POLLERR | POLLNVAL));
}

/// Synchronous non-streaming forward.  UpstreamClient's process-wide watchdog
/// monitors the downstream socket without creating a per-request thread.
UpstreamClient::ForwardResult
forward_once(UpstreamClient &upstream, const std::string &body,
             const std::string &content_type, const UpstreamCandidate &c,
             const UpstreamTarget &target, int client_sock) {
    auto opts = target.opts;
    opts.downstream_socket = client_sock;
    return upstream.forward(
        "POST", c.account().base_url, c.key(),
        target.path, body, content_type, nullptr, opts);
}

UpstreamClient::ForwardResult forward_endpoint_attempt(
    UpstreamClient &upstream, const EndpointPolicy &policy,
    const UpstreamCandidate &candidate, const std::string &body,
    const std::string &content_type, int remaining_budget_ms,
    const Database::TimeoutConfig &timeouts, int client_socket,
    const std::function<bool(const char *, size_t)> &on_chunk,
    const std::function<void(ForwardOptions &)> &configure) {
    auto bounded = timeouts;
    const int seconds = std::max(1, (remaining_budget_ms + 999) / 1000);
    if (policy.timeout_class == TimeoutClass::Streaming)
        bounded.streaming_first_byte_timeout = std::min(
            bounded.streaming_first_byte_timeout, seconds);
    else
        bounded.non_streaming_timeout = std::min(
            bounded.non_streaming_timeout, seconds);
    auto target = resolve_upstream_target(
        policy,
        candidate.account().api_format, candidate.account().base_url,
        candidate.account().endpoint_path, candidate.account().auth_header,
        bounded);
    target.opts.attempt_budget_ms = remaining_budget_ms;
    target.opts.downstream_socket = client_socket;
    if (configure) configure(target.opts);
    const std::string method = policy.http_method == HttpMethod::Get
        ? "GET" : "POST";
    return upstream.forward(method, candidate.account().base_url,
                            candidate.key(), target.path, body, content_type,
                            on_chunk, target.opts);
}

Database::AttemptInfo attempt_info(
    const UpstreamCandidate &candidate,
    const UpstreamClient::ForwardResult &result,
    int semantic_ttft_ms) {
    Database::AttemptInfo out;
    out.account_id = candidate.account().id;
    out.upstream_id = candidate.account().upstream_id;
    out.upstream_key_id = candidate.key_slot_id;
    out.status_code = result.status_code;
    out.duration_ms = result.duration_ms;
    out.dns_ms = result.dns_ms;
    out.connect_ms = result.connect_ms;
    out.tls_ms = result.tls_ms;
    out.lease_wait_ms = result.lease_wait_ms;
    out.first_byte_ms = result.first_byte_ms;
    out.connection_reused = result.connection_reused;
    out.ttft_ms = semantic_ttft_ms;
    out.is_timeout = result.is_timeout;
    out.error = result.error;
    return out;
}

// ── handle_chat_request ──────────────────────────────────────────────────

/// Entry point for /v1/chat/completions, /v1/messages and /v1/responses.
/// The harness format comes from the incoming request URL path; when it matches
/// the account's api_format we use the passthrough fast path, otherwise we
/// convert via the IR codecs.
///
/// Candidate handling: plain accounts have one candidate; aggregate accounts
/// may have several (same model → several upstreams, priority order).  The
/// first candidate that is not cooling down and has a free concurrency slot
/// wins.  Non-streaming requests fall back to the next candidate when the
/// upstream answers 429 (plan accounts then cool down for 5h) or 5xx before
/// anything was sent to the client.  Streaming requests are one-shot: once
/// the chunked provider starts, headers are committed, so no fallback.
