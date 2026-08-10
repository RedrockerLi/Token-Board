#pragma once

#include "proxy_server.h"
#include "attempt_executor.h"
#include "endpoint_policy.h"
#include "error_render.h"
#include "format_common.h"
#include "logging.h"
#include "request_context.h"
#include "request_body_cache.h"
#include "request_timing.h"
#include "router.h"
#include "think_filter.h"
#include "upstream_client.h"
#include "usage_tracker.h"

#define CPPHTTPLIB_OPENSSL_SUPPORT
#include "httplib.h"
#include "json.hpp"

#include <atomic>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <numeric>
#include <poll.h>
#include <sys/socket.h>
#include <thread>
#include <utility>

using json = nlohmann::json;

class GateLease {
public:
    GateLease(AccountGate &gate, int key_slot_id) noexcept
        : gate_(&gate), key_slot_id_(key_slot_id) {}
    GateLease(const GateLease &) = delete;
    GateLease &operator=(const GateLease &) = delete;
    ~GateLease() { release(); }
    void release() {
        if (!gate_) return;
        gate_->release(key_slot_id_);
        gate_ = nullptr;
    }
private:
    AccountGate *gate_;
    int key_slot_id_;
};

template <typename F>
class ScopeExit {
public:
    explicit ScopeExit(F fn) : fn_(std::move(fn)) {}
    ScopeExit(const ScopeExit &) = delete;
    ScopeExit &operator=(const ScopeExit &) = delete;
    ~ScopeExit() { run_now(); }
    void run_now() {
        if (!active_) return;
        active_ = false;
        fn_();
    }
private:
    F fn_;
    bool active_ = true;
};

template <typename F>
ScopeExit<F> make_scope_exit(F fn) {
    return ScopeExit<F>(std::move(fn));
}

struct AuthResult {
    bool success = false;
    Router::RouteResult route;
    std::string error_json;
};

std::string affinity_scope(int local_key_id, ir::ApiFormat harness);
bool strict_terminal_enabled();
size_t affinity_start(SessionAffinity &affinity, const std::string &scope,
                      const std::string &session_id,
                      const std::vector<UpstreamCandidate> &candidates);
std::string response_id_from_body(const std::string &body);
std::string json_error(const std::string &message, int code);
int stream_error_status(const json &error);
std::string stream_error_message(const json &error);
std::string resolve_auth_scheme(const std::string &api_format,
                                 const std::string &configured);
AuthResult extract_and_route(const httplib::Request &request, Router &router);
std::vector<UpstreamCandidate> resolve_candidates_uncached(
    Router &router, const Router::RouteResult &route, std::string &model);
ir::ApiFormat harness_format_from_path(const std::string &path);
std::vector<CandidateRequestBody> candidate_request_bodies(
    const std::shared_ptr<const std::string> &original_body,
    const json &parsed,
    const std::string &requested_model,
    const std::vector<UpstreamCandidate> &candidates,
    ir::ApiFormat client_format);
struct UpstreamTarget {
    std::string path;
    ForwardOptions opts;
};
UpstreamTarget resolve_upstream_target(
    const EndpointPolicy &policy,
    const std::string &api_format, const std::string &base_url,
    const std::string &endpoint_path, const std::string &auth_header,
    const Database::TimeoutConfig &timeouts);
UpstreamClient::ForwardResult forward_endpoint_attempt(
    UpstreamClient &upstream, const EndpointPolicy &policy,
    const UpstreamCandidate &candidate, const std::string &body,
    const std::string &content_type, int remaining_budget_ms,
    const Database::TimeoutConfig &timeouts, int client_socket,
    const std::function<bool(const char *, size_t)> &on_chunk = nullptr,
    const std::function<void(ForwardOptions &)> &configure = nullptr);
bool clamp_to_remaining_budget(
    Database::TimeoutConfig &timeouts,
    std::chrono::steady_clock::time_point deadline, bool streaming);
std::optional<UsageTracker::UsageInfo> parse_usage_for_format(
    const std::string &api_format, const std::string &body);
UsageTracker::UsageInfo usage_from_ir(const ir::Usage &usage,
                                     ir::ApiFormat upstream_format);
bool client_disconnected(const httplib::Request &request,
                         std::uint64_t inflight_id,
                         const std::string &model);
bool client_socket_gone(int socket);
UpstreamClient::ForwardResult forward_once(
    UpstreamClient &upstream, const std::string &body,
    const std::string &content_type, const UpstreamCandidate &candidate,
    const UpstreamTarget &target, int client_socket);
Database::AttemptInfo attempt_info(
    const UpstreamCandidate &candidate,
    const UpstreamClient::ForwardResult &result,
    int semantic_ttft_ms = -1);
