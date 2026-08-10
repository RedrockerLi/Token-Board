#pragma once

#include "db.h"

#include <cstddef>
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
    int key_slot_id = -1;
    std::shared_ptr<const std::string> upstream_model_ref;
    int priority_group = 0;

    const Database::AccountInfo &account() const { return *account_ref; }
    const std::string &key() const { return key_slot_ref->key_value; }
    const std::string &upstream_model() const { return *upstream_model_ref; }
};

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
    return order;
}

inline bool candidate_failure_retryable(int status_code) {
    return status_code == 401 || status_code == 403 ||
           status_code == 429 || status_code >= 500;
}
