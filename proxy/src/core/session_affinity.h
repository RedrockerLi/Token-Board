#pragma once

#include <chrono>
#include <cstdint>
#include <list>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

/// Bounded, process-local session to upstream-credential binding.
///
/// A binding exists only after a key successfully served the session; the
/// request order then starts that group at the bound key so its cache stays
/// warm.  Everything else about ordering (pressure, cost, exhaustion) lives
/// in candidate_order — this class is a binding store, nothing more.
class SessionAffinity {
public:
    SessionAffinity() = default;

    /// Index (into `key_slot_ids`) of the session's live binding; nullopt for
    /// an empty/expired session, an unknown session or a bound key that is
    /// not part of this candidate set.
    std::optional<size_t> bound_index(const std::string &scope,
                                      const std::string &session_id,
                                      const std::vector<int> &key_slot_ids) {
        if (session_id.empty() || key_slot_ids.empty()) return std::nullopt;
        const std::string key = digest(scope, session_id);
        std::lock_guard<std::mutex> lock(mutex_);
        auto now = std::chrono::steady_clock::now();
        auto it = bindings_.find(key);
        if (it == bindings_.end()) return std::nullopt;
        if (now - it->second.last_seen > ttl_) {
            lru_.erase(it->second.lru_it);
            bindings_.erase(it);
            return std::nullopt;
        }
        it->second.last_seen = now;
        lru_.splice(lru_.begin(), lru_, it->second.lru_it);
        for (size_t i = 0; i < key_slot_ids.size(); ++i)
            if (key_slot_ids[i] == it->second.key_slot_id) return i;
        return std::nullopt;
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
};
