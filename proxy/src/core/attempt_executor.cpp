#include "attempt_executor.h"

#include <algorithm>

namespace {
Database::AttemptInfo to_attempt(const UpstreamCandidate &candidate,
                                 const UpstreamClient::ForwardResult &result) {
    Database::AttemptInfo out;
    out.account_id = candidate.account().id;
    out.upstream_id = candidate.account().upstream_id;
    out.upstream_key_id = candidate.key_slot_id;
    out.status_code = result.status_code;
    out.duration_ms = result.duration_ms;
    out.ttft_ms = result.ttft_ms;
    out.is_timeout = result.is_timeout;
    out.error = result.error;
    out.dns_ms = result.dns_ms;
    out.connect_ms = result.connect_ms;
    out.tls_ms = result.tls_ms;
    out.lease_wait_ms = result.lease_wait_ms;
    out.first_byte_ms = result.first_byte_ms;
    out.connection_reused = result.connection_reused;
    return out;
}
}  // namespace

bool AttemptExecutor::acquire(const UpstreamCandidate &candidate) const {
    return gate_.try_acquire_eligible(candidate.key_slot_id,
                                       candidate.account().max_concurrency);
}

void AttemptExecutor::complete(
    const UpstreamCandidate &candidate,
    const UpstreamClient::ForwardResult &result,
    bool downstream_gone) const {
    if (result.success && result.status_code >= 200 &&
        result.status_code < 300) {
        gate_.mark_success(candidate.key_slot_id);
    } else if (!downstream_gone &&
               candidate_failure_retryable(result.status_code)) {
        gate_.record_failure(candidate.key_slot_id,
               candidate.account().extended_usage_limit_cooldown,
                             result.usage_limit, result.status_code);
    }
    gate_.release(candidate.key_slot_id);
}

bool AttemptExecutor::should_retry(
    const UpstreamClient::ForwardResult &result, bool downstream_gone,
    bool has_next) noexcept {
    return has_next && !downstream_gone &&
           candidate_failure_retryable(result.status_code);
}

AttemptOutcome AttemptExecutor::execute(const ExecutionRequest &request) const {
    std::uint64_t inflight_id = 0;
    Forward forward = request.forward;
    if (request.inflight_start) {
        // Start the live-request entry on the first attempt actually
        // forwarded, then finish it after the loop.  Handlers no longer own an
        // inflight_id + scope guard; streaming passes empty hooks and keeps its
        // own bookkeeping.
        const Forward base = request.forward;
        forward = [&, base](const AttemptRequest &attempt) {
            if (inflight_id == 0)
                inflight_id = request.inflight_start(attempt.candidate.upstream_model());
            return base(attempt);
        };
    }
    AttemptOutcome outcome = run(*request.candidates, request.order,
                                 request.deadline, request.budget_seconds,
                                 forward, request.disconnected);
    if (inflight_id != 0 && request.inflight_end)
        request.inflight_end(inflight_id);
    return outcome;
}

AttemptOutcome AttemptExecutor::run(
    const std::vector<UpstreamCandidate> &candidates,
    const std::vector<std::size_t> &order,
    std::chrono::steady_clock::time_point deadline, int budget_seconds,
    const Forward &forward, const Disconnected &disconnected) const {
    AttemptOutcome outcome;
    for (std::size_t position = 0; position < order.size(); ++position) {
        const std::size_t index = order[position];
        if (index >= candidates.size()) continue;
        const auto &candidate = candidates[index];
        if (!acquire(candidate))
            continue;

        const auto now = std::chrono::steady_clock::now();
        const auto remaining = std::chrono::duration_cast<
            std::chrono::milliseconds>(deadline - now).count();
        if (remaining <= 0) {
            gate_.release(candidate.key_slot_id);
            outcome.result.status_code = 504;
            outcome.result.is_timeout = true;
            outcome.result.timeout_secs = budget_seconds;
            outcome.result.error = "request retry budget exhausted";
            outcome.budget_exhausted = true;
            break;
        }

        outcome.result = forward(AttemptRequest{candidate, index, remaining});
        outcome.last_attempted = &candidate;
        outcome.attempts.push_back(to_attempt(candidate, outcome.result));
        const bool downstream_gone = disconnected(outcome.result);
        outcome.successful = outcome.result.success &&
            outcome.result.status_code >= 200 &&
            outcome.result.status_code < 300;
        complete(candidate, outcome.result, downstream_gone);

        const bool retry = should_retry(outcome.result, downstream_gone,
                                        position + 1 < order.size());
        if (retry) continue;
        outcome.used = &candidate;
        break;
    }
    return outcome;
}
