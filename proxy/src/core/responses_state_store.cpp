#include "responses_state_store.h"
#include "logging.h"

#include <algorithm>

bool ResponsesStateStore::lookup(const std::string &response_id,
                                 std::vector<nlohmann::json> &items) const {
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = entries_.find(response_id);
    if (it == entries_.end()) return false;
    items = it->second.items;
    return true;
}

bool ResponsesStateStore::record(
    const std::string &response_id,
    const std::vector<nlohmann::json> &input_items,
    const std::vector<nlohmann::json> &output_items) {
    if (response_id.empty()) return false;

    Entry entry;
    entry.input_items = input_items;
    entry.output_items = output_items;
    entry.items.reserve(input_items.size() + output_items.size());
    for (const auto &item : input_items) entry.items.push_back(item);
    for (const auto &item : output_items) entry.items.push_back(item);
    for (const auto &item : entry.items) entry.bytes += item.dump().size();
    if (entry.bytes > kMaxBytes) {
        TB_LOG_WARN("[ResponsesState] response %s exceeds 64 MiB; not cached\n",
                    response_id.c_str());
        return false;
    }

    std::lock_guard<std::mutex> lock(mutex_);
    auto old = entries_.find(response_id);
    if (old != entries_.end()) {
        bytes_ -= old->second.bytes;
        order_.erase(std::find(order_.begin(), order_.end(), response_id));
        entries_.erase(old);
    }
    while (!order_.empty() &&
           (order_.size() >= kMaxResponses || bytes_ + entry.bytes > kMaxBytes)) {
        const std::string victim = order_.front();
        order_.pop_front();
        auto it = entries_.find(victim);
        if (it != entries_.end()) {
            bytes_ -= it->second.bytes;
            entries_.erase(it);
        }
    }
    if (bytes_ + entry.bytes > kMaxBytes) return false;
    bytes_ += entry.bytes;
    order_.push_back(response_id);
    entries_.emplace(response_id, std::move(entry));
    return true;
}

std::size_t ResponsesStateStore::size() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return entries_.size();
}

std::size_t ResponsesStateStore::bytes() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return bytes_;
}
