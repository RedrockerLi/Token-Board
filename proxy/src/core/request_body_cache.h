#pragma once

#include "candidate_selection.h"
#include "json.hpp"

#include <string>
#include <functional>
#include <unordered_map>

class RequestBodyCache {
public:
    RequestBodyCache(const std::string &original,
                     const nlohmann::json &parsed,
                     std::string requested_model,
                     std::string client_format = "openai")
        : original_(original), parsed_(parsed),
          requested_model_(std::move(requested_model)),
          client_format_(std::move(client_format)) {}

    const std::string &for_candidate(const UpstreamCandidate &candidate) {
        if (candidate.upstream_model() == requested_model_) return original_;
        const std::string cache_key = candidate.account().api_format + "\n" +
                                      candidate.upstream_model();
        auto found = rewritten_.find(cache_key);
        if (found != rewritten_.end()) return found->second;
        auto changed = parsed_;
        changed["model"] = candidate.upstream_model();
        return rewritten_.emplace(cache_key,
                                  changed.dump()).first->second;
    }

    template <typename Builder>
    const std::string &for_transformed(const std::string &target_format,
                                       const std::string &target_model,
                                       Builder &&builder) {
        if (target_format == client_format_ && target_model == requested_model_)
            return original_;
        const std::string cache_key = target_format + "\n" + target_model;
        auto found = rewritten_.find(cache_key);
        if (found != rewritten_.end()) return found->second;
        return rewritten_.emplace(cache_key, builder()).first->second;
    }

private:
    const std::string &original_;
    const nlohmann::json &parsed_;
    std::string requested_model_;
    std::string client_format_;
    std::unordered_map<std::string, std::string> rewritten_;
};
