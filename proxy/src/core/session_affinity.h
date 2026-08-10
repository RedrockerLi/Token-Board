#pragma once

#include "key_cost_ledger.h"

#include <chrono>
#include <cstdint>
#include <list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

/// Bounded, process-local session to upstream-credential affinity.
class SessionAffinity {
public:
    SessionAffinity() = default;
    explicit SessionAffinity(KeyCostLedger *ledger) : ledger_(ledger) {}

    size_t preferred_index(const std::string &scope,
                           const std::string &session_id,
                           const std::vector<int> &key_slot_ids) {
        if (session_id.empty() || key_slot_ids.empty()) return 0;
        const std::string key = digest(scope, session_id);
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(key);
        if (it != bindings_.end()) {
            if (now - it->second.last_seen > ttl_) {
                lru_.erase(it->second.lru_it);
                bindings_.erase(it);
            } else {
                it->second.last_seen = now;
                lru_.splice(lru_.begin(), lru_, it->second.lru_it);
                for (size_t i = 0; i < key_slot_ids.size(); ++i)
                    if (key_slot_ids[i] == it->second.key_slot_id) return i;
            }
        }
        if (ledger_) return ledger_->lowest_cost_index(key_slot_ids);
        size_t best = 0;
        uint64_t best_score = 0;
        for (size_t i = 0; i < key_slot_ids.size(); ++i) {
            const uint64_t score = hash64(
                key + "#" + std::to_string(key_slot_ids[i]));
            if (i == 0 || score > best_score) {
                best = i;
                best_score = score;
            }
        }
        return best;
    }

    void bind(const std::string &scope, const std::string &session_id,
              int key_slot_id) {
        if (session_id.empty()) return;
        const std::string key = digest(scope, session_id);
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(key);
        if (it != bindings_.end()) {
            it->second.key_slot_id = key_slot_id;
            it->second.last_seen = now;
            lru_.splice(lru_.begin(), lru_, it->second.lru_it);
            return;
        }
        while (bindings_.size() >= max_ && !lru_.empty()) {
            bindings_.erase(lru_.back());
            lru_.pop_back();
        }
        lru_.push_front(key);
        bindings_.emplace(key, Entry{key_slot_id, now, lru_.begin()});
    }

private:
    static uint64_t hash64(const std::string &value) {
        uint64_t h = 1469598103934665603ULL;
        for (unsigned char c : value) {
            h ^= c;
            h *= 1099511628211ULL;
        }
        return h;
    }
    static std::string digest(const std::string &scope,
                              const std::string &session_id) {
        return std::to_string(hash64(scope + "\x1f" + session_id));
    }
    struct Entry {
        int key_slot_id;
        std::chrono::steady_clock::time_point last_seen;
        std::list<std::string>::iterator lru_it;
    };

    static constexpr size_t max_ = 100000;
    std::chrono::hours ttl_{24};
    std::mutex mutex_;
    std::list<std::string> lru_;
    std::unordered_map<std::string, Entry> bindings_;
    KeyCostLedger *ledger_ = nullptr;
};
