#pragma once

#include "account_gate.h"
#include "candidate_selection.h"
#include "upstream_client.h"

#include <chrono>
#include <cstddef>
#include <functional>
#include <vector>

struct AttemptRequest {
    const UpstreamCandidate &candidate;
    std::size_t candidate_index = 0;
    int64_t remaining_budget_ms = 0;
};

struct AttemptOutcome {
    UpstreamClient::ForwardResult result;
    const UpstreamCandidate *used = nullptr;
    const UpstreamCandidate *last_attempted = nullptr;
    std::vector<Database::AttemptInfo> attempts;
    // True only when the selected attempt produced a successful 2xx response.
    // `used` intentionally still identifies the terminal candidate for an
    // upstream error, so callers can record that failed attempt accurately.
    bool successful = false;
    bool budget_exhausted = false;
};

class AttemptExecutor {
public:
    using Forward = std::function<UpstreamClient::ForwardResult(
        const AttemptRequest &)>;
    using Disconnected = std::function<bool(
        const UpstreamClient::ForwardResult &)>;

    // Everything a request needs from the candidate loop, packaged so handlers
    // stop re-assembling order/deadline/budget and the inflight bookkeeping on
    // their own.  `order` and `deadline` stay handler-computed because the
    // ordering policy (session affinity vs plain iota) and the deadline base
    // (t0 vs now) differ per endpoint.
    struct ExecutionRequest {
        const std::vector<UpstreamCandidate> *candidates = nullptr;
        std::vector<std::size_t> order;
        std::chrono::steady_clock::time_point deadline;
        int budget_seconds = 0;
        // Optional live-request tracking.  When inflight_start is set, execute()
        // starts it on the first forwarded attempt and ends it after the loop,
        // so handlers no longer carry a scope guard + sentinel id themselves.
        std::function<std::uint64_t(const std::string &model)> inflight_start;
        std::function<void(std::uint64_t)> inflight_end;
        Forward forward;
        Disconnected disconnected;
    };

    explicit AttemptExecutor(AccountGate &gate) : gate_(gate) {}

    AttemptOutcome execute(const ExecutionRequest &request) const;

    // Streaming callbacks have longer-lived response state than ordinary
    // Forward callbacks, but use these same gate/cooldown transitions.
    bool acquire(const UpstreamCandidate &candidate) const;
    void complete(const UpstreamCandidate &candidate,
                  const UpstreamClient::ForwardResult &result,
                  bool downstream_gone) const;
    static bool should_retry(const UpstreamClient::ForwardResult &result,
                             bool downstream_gone, bool has_next) noexcept;

private:
    AttemptOutcome run(
        const std::vector<UpstreamCandidate> &candidates,
        const std::vector<std::size_t> &order,
        std::chrono::steady_clock::time_point deadline,
        int budget_seconds, const Forward &forward,
        const Disconnected &disconnected) const;
    AccountGate &gate_;
};
