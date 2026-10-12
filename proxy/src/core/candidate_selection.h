#pragma once

#include "db.h"
#include "routing_snapshot.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

struct UpstreamCandidate {
    // Candidate instances are request-local, but the expensive account and
    // credential data belongs to the immutable routing snapshot.  Keep only
    // shared read-only references here so expanding one account to several
    // key slots does not copy base URLs, endpoint paths or auth policy per
    // attempt.
    std::shared_ptr<const Database::AccountInfo> account_ref;
    std::shared_ptr<const Database::KeySlot> key_slot_ref;
    // Null when this credential has no usage snapshot: the candidate then has
    // no deadline and is never treated as exhausted.
    std::shared_ptr<const routing::CredentialUsage> usage_ref;
    int key_slot_id = -1;
    std::shared_ptr<const std::string> upstream_model_ref;
    int priority_group = 0;

    const Database::AccountInfo &account() const { return *account_ref; }
    const std::string &key() const { return key_slot_ref->key_value; }
    const std::string &upstream_model() const { return *upstream_model_ref; }
    const routing::CredentialUsage *usage() const { return usage_ref.get(); }
};

// Usage-aware scheduling thresholds (upstreams.subtype = 'opencode-go').
// A window at or below kUsablePercent still has quota worth spending before it
// resets; a window at or above kExhaustedPercent marks the credential as spent;
// kUsageLeadGap keeps the order stable when the head start of the
// soonest-resetting key is too small to matter.
inline constexpr int kUsablePercent = 70;
inline constexpr int kExhaustedPercent = 100;
inline constexpr std::int64_t kUsageLeadGapSeconds = 30 * 60;

/// A candidate is exhausted when any reported window has reached 100%.  It is
/// never dropped — the provider stays authoritative — but it is scheduled last.
inline bool candidate_usage_exhausted(const UpstreamCandidate &candidate) {
    const auto *usage = candidate.usage();
    return usage != nullptr && usage->exhausted;
}

/// Earliest reset (epoch seconds) among the windows that still have quota; 0
/// when the candidate has no usable window or no usage data at all.  The
/// soonest-resetting window is the one whose unused quota expires first.
/// A reported percentage never decides whether a window competes: percent=0
/// means "under 1% used", so such a window keeps its deadline like any
/// other.  Only a window above kUsablePercent or without a parseable reset
/// supplies nothing.  Exhausted candidates report no deadline: they are
/// scheduled last, so they can never act as the lead.
inline std::int64_t candidate_usage_deadline(
    const UpstreamCandidate &candidate) {
    const auto *usage = candidate.usage();
    if (usage == nullptr || usage->exhausted) return 0;
    std::int64_t deadline = 0;
    for (const auto &window : usage->windows) {
        if (window.percent > kUsablePercent || window.resets_at <= 0) continue;
        if (deadline == 0 || window.resets_at < deadline)
            deadline = window.resets_at;
    }
    return deadline;
}

/// Usage-aware start index.  When the soonest-resetting usable window leads
/// the next distinct reset by at least kUsageLeadGapSeconds, that candidate
/// starts the rotation — its quota would otherwise expire unused.  Everything
/// else (no usage data, a single deadline, or a gap below the threshold) keeps
/// the caller's affinity-preferred index, so candidates with no usage data are
/// unaffected.
inline std::size_t usage_lead_start(
    const std::vector<UpstreamCandidate> &candidates,
    std::size_t preferred_index) {
    if (candidates.size() < 2) return preferred_index;
    std::int64_t smallest = 0;
    std::int64_t second = 0;
    for (const auto &candidate : candidates) {
        const std::int64_t deadline = candidate_usage_deadline(candidate);
        if (deadline == 0) continue;
        if (smallest == 0 || deadline < smallest) {
            second = smallest;
            smallest = deadline;
        } else if (deadline != smallest && (second == 0 || deadline < second)) {
            second = deadline;
        }
    }
    if (smallest == 0 || second == 0 ||
        second - smallest < kUsageLeadGapSeconds)
        return preferred_index;
    // Ties (same deadline) keep the affinity choice when it is one of them;
    // otherwise the first tied candidate in candidate order starts.
    if (preferred_index < candidates.size() &&
        candidate_usage_deadline(candidates[preferred_index]) == smallest)
        return preferred_index;
    for (std::size_t i = 0; i < candidates.size(); ++i)
        if (candidate_usage_deadline(candidates[i]) == smallest) return i;
    return preferred_index;
}

inline std::vector<size_t> candidate_order(
    const std::vector<UpstreamCandidate> &candidates,
    size_t preferred_index, size_t rr_offset) {
    std::vector<size_t> order;
    if (candidates.empty()) return order;
    size_t i = 0;
    while (i < candidates.size()) {
        const int group = candidates[i].priority_group;
        const size_t begin = i;
        while (i < candidates.size() &&
               candidates[i].priority_group == group) ++i;
        const size_t size = i - begin;
        const size_t start = (preferred_index >= begin && preferred_index < i)
            ? (preferred_index - begin) % size
            : rr_offset % size;
        for (size_t k = 0; k < size; ++k)
            order.push_back(begin + (start + k) % size);
    }
    // Usage-aware partition: an exhausted credential is tried only after every
    // candidate that may still have quota — including candidates of a
    // lower-priority group, whose attempt would otherwise be spent on a key
    // the provider already reports as spent.  The partition is stable, so the
    // rotation order inside both halves is preserved and nothing is dropped.
    std::stable_partition(order.begin(), order.end(), [&](size_t index) {
        return !candidate_usage_exhausted(candidates[index]);
    });
    return order;
}

inline bool candidate_failure_retryable(int status_code) {
    return status_code == 401 || status_code == 403 ||
           status_code == 429 || status_code >= 500;
}
