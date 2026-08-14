#pragma once

#include "json.hpp"

#include <cstddef>
#include <deque>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

/// Process-local bounded Responses state.  It stores wire-shaped Items rather
/// than text so media and opaque reasoning remain replayable without ever
/// becoming prompt text.
class ResponsesStateStore {
public:
    static constexpr std::size_t kMaxResponses = 512;
    static constexpr std::size_t kMaxBytes = 64u * 1024u * 1024u;

    bool lookup(const std::string &response_id,
                std::vector<nlohmann::json> &items) const;
    bool record(const std::string &response_id,
                const std::vector<nlohmann::json> &input_items,
                const std::vector<nlohmann::json> &output_items);
    std::size_t size() const;
    std::size_t bytes() const;

private:
    struct Entry {
        std::vector<nlohmann::json> items;
        std::vector<nlohmann::json> input_items;
        std::vector<nlohmann::json> output_items;
        std::size_t bytes = 0;
    };

    mutable std::mutex mutex_;
    std::unordered_map<std::string, Entry> entries_;
    std::deque<std::string> order_;
    std::size_t bytes_ = 0;
};
