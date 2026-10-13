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

AccountGate::KeyAcquireResult AttemptExecutor::acquire(
    const UpstreamCandidate &candidate) const {
    return gate_.try_acquire_eligible(candidate.key_slot_id,
                                      candidate.account().max_concurrency);
}

void AttemptExecutor::complete(
    const UpstreamCandidate &candidate,
    const UpstreamClient::ForwardResult &result) const {
    const int slot = candidate.key_slot_id;
    const std::int64_t now = wall_now_epoch();
    if (result.success) {
        // A completed call is ground truth that clears the short-lived
        // failure states; the quota cooldown is only lifted by reset
        // evidence (a slot inside the cooldown is skipped and cannot
        // succeed here).
        gate_.note_success(slot);
    } else if (result.status_code == 429 && result.usage_limit &&
               candidate.account().extended_usage_limit_cooldown) {
        // Explicit subscription quota exhaustion: prefer the trusted window
        // reset over the fixed fallback and record the evidence.
        const auto plan = quota_cooldown_plan(candidate, result.limit_window,
                                              now);
        gate_.mark_subscription_cooldown(
            slot,
            std::chrono::seconds(std::max<std::int64_t>(
                1, plan.until_wall - now)),
            AccountGate::CooldownEvidence{plan.window, plan.evidence_resets_at,
                                          now});
    } else if (result.status_code == 429) {
        // An ordinary 429 is rate limiting, not a subscription quota: a short
        // avoid honoring the upstream's Retry-After, never the retained
        // cooldown.
        const int seconds = result.retry_after_seconds > 0
            ? std::min(result.retry_after_seconds, 86400)
            : static_cast<int>(std::chrono::duration_cast<std::chrono::seconds>(
                                   AccountGate::RATE_LIMIT_AVOID)
                                   .count());
        gate_.mark_rate_limited(slot, std::chrono::seconds(seconds));
    } else if (result.status_code == 401 || result.status_code == 403) {
        gate_.mark_auth_error(slot);
    } else if ((result.status_code >= 500 || result.is_timeout) &&
               result.failure_kind != UpstreamFailureKind::RequestConversion) {
        // Provider faults (5xx, transport timeouts) get a short avoid that a
        // later success clears; local conversion failures never blame the
        // key.
        gate_.mark_provider_fault(slot);
    }
    gate_.release(slot);
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
                                 forward, request.disconnected,
                                 request.attempt_failed,
                                 request.candidate_skipped);
    if (inflight_id != 0 && request.inflight_end)
        request.inflight_end(inflight_id);
    return outcome;
}

AttemptOutcome AttemptExecutor::run(
    const std::vector<UpstreamCandidate> &candidates,
    const std::vector<std::size_t> &order,
    std::chrono::steady_clock::time_point deadline, int budget_seconds,
    const Forward &forward, const Disconnected &disconnected,
    const AttemptFailed &attempt_failed,
    const CandidateSkipped &candidate_skipped) const {
    AttemptOutcome outcome;
    bool saw_concurrency_full = false;
    bool saw_subscription_cooldown = false;
    bool saw_rate_limited = false;
    bool saw_key_unavailable = false;
    for (std::size_t position = 0; position < order.size(); ++position) {
        const std::size_t index = order[position];
        if (index >= candidates.size()) continue;
        const auto &candidate = candidates[index];
        const auto acquire_result = acquire(candidate);
        if (acquire_result != AccountGate::KeyAcquireResult::kAcquired) {
            if (candidate_skipped)
                candidate_skipped(candidate, acquire_result);
            saw_concurrency_full = saw_concurrency_full ||
                acquire_result == AccountGate::KeyAcquireResult::kConcurrencyFull;
            saw_subscription_cooldown = saw_subscription_cooldown ||
                acquire_result == AccountGate::KeyAcquireResult::kSubscriptionCooldown;
            if (acquire_result == AccountGate::KeyAcquireResult::kRateLimited) {
                saw_rate_limited = true;
                const int wait =
                    gate_.rate_limit_avoid_seconds(candidate.key_slot_id);
                if (wait > 0 &&
                    (outcome.no_candidate_retry_after == 0 ||
                     wait < outcome.no_candidate_retry_after))
                    outcome.no_candidate_retry_after = wait;
            } else if (acquire_result == AccountGate::KeyAcquireResult::kAuthError ||
                       acquire_result ==
                           AccountGate::KeyAcquireResult::kProviderFault) {
                saw_key_unavailable = true;
            }
            continue;
        }
        // The lease releases the slot if this iteration unwinds (an exception
        // inside a forward callback); complete() releases it on the normal
        // paths and the lease is disarmed afterwards.
        GateLease lease(gate_, candidate.key_slot_id);

        const auto now = std::chrono::steady_clock::now();
        const auto remaining = std::chrono::duration_cast<
            std::chrono::milliseconds>(deadline - now).count();
        if (remaining <= 0) {
            lease.disarm();
            gate_.release(candidate.key_slot_id);
            outcome.result.status_code = 504;
            outcome.result.is_timeout = true;
            outcome.result.failure_kind = UpstreamFailureKind::Timeout;
            outcome.result.timeout_secs = budget_seconds;
            outcome.result.error = "request retry budget exhausted";
            outcome.budget_exhausted = true;
            if (attempt_failed)
                attempt_failed(
                    AttemptRequest{candidate, index, remaining}, outcome.result,
                    outcome.attempts.size() + 1, false);
            break;
        }

        outcome.result = forward(AttemptRequest{candidate, index, remaining});
        outcome.last_attempted = &candidate;
        outcome.attempts.push_back(to_attempt(candidate, outcome.result));
        const bool downstream_gone = disconnected(outcome.result);
        outcome.successful = outcome.result.success &&
            outcome.result.status_code >= 200 &&
            outcome.result.status_code < 300;
        complete(candidate, outcome.result);
        lease.disarm();

        const bool retry = should_retry(outcome.result, downstream_gone,
                                        position + 1 < order.size());
        if (!outcome.successful && attempt_failed)
            attempt_failed(AttemptRequest{candidate, index, remaining},
                           outcome.result, outcome.attempts.size(), retry);
        if (retry) continue;
        outcome.used = &candidate;
        break;
    }
    if (outcome.attempts.empty() && !outcome.budget_exhausted) {
        outcome.no_candidate_reason = saw_concurrency_full
            ? NoCandidateReason::kLocalCapacity
            : saw_subscription_cooldown
                ? NoCandidateReason::kProviderQuotaCooldown
                : saw_rate_limited
                    ? NoCandidateReason::kKeyRateLimited
                    : saw_key_unavailable ? NoCandidateReason::kKeyUnavailable
                                          : NoCandidateReason::kNone;
    }
    return outcome;
}
