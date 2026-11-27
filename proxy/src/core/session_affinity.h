#pragma once

#include <chrono>
#include <cstddef>
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
        const std::string &key = digest_key(digest(scope, session_id));
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
        const std::string &key = digest_key(digest(scope, session_id));
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
    /// FNV-1a over one string's bytes, continuing from `h`.
    static uint64_t hash_step(uint64_t h, const std::string &value) {
        for (unsigned char c : value) {
            h ^= c;
            h *= 1099511628211ULL;
        }
        return h;
    }
    /// Incremental FNV-1a over scope, the 0x1f separator and the session id.
    /// Feeding the parts in order yields the same 64-bit value as hashing
    /// `scope + "\x1f" + session_id`, without building that temporary string
    /// (which used to cost two allocations per call).
    static uint64_t digest(const std::string &scope,
                           const std::string &session_id) {
        uint64_t h = hash_step(1469598103934665603ULL, scope);
        h ^= 0x1f;
        h *= 1099511628211ULL;
        return hash_step(h, session_id);
    }
    /// Decimal key for `value`, rendered into a per-thread buffer.  The map is
    /// keyed by that string, so a warmed buffer makes the per-request lookup
    /// allocation-free; std::to_string allocated on every call because a
    /// 20-digit hash never fits libstdc++'s 15-byte small-string buffer.  The
    /// returned reference stays valid until the next call on this thread.
    static const std::string &digest_key(uint64_t value) {
        static thread_local std::string buffer;
        char digits[20];
        std::size_t count = 0;
        do {
            digits[count++] = static_cast<char>('0' + value % 10);
            value /= 10;
        } while (value != 0);
        buffer.clear();
        if (buffer.capacity() < count) buffer.reserve(count);
        while (count > 0) buffer.push_back(digits[--count]);
        return buffer;
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
