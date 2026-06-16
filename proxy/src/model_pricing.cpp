#include "model_pricing.h"
#include "db.h"

#include <algorithm>
#include <cstring>

// ── glob_match — shell-style glob with * and ? ────────────────────────────

bool glob_match(const std::string &pattern, const std::string &text) {
    size_t pi = 0, mi = 0;
    size_t star_pos = std::string::npos;
    size_t match_pos = 0;

    while (mi < text.size()) {
        if (pi < pattern.size() &&
            (pattern[pi] == '?' ||
             tolower(pattern[pi]) == tolower(text[mi]))) {
            ++pi; ++mi;
        } else if (pi < pattern.size() && pattern[pi] == '*') {
            star_pos = pi; match_pos = mi; ++pi;
        } else if (star_pos != std::string::npos) {
            pi = star_pos + 1; match_pos++; mi = match_pos;
        } else {
            return false;
        }
    }
    while (pi < pattern.size() && pattern[pi] == '*') ++pi;
    return pi == pattern.size();
}

// ── ModelPricing::match_pattern (delegates to glob_match) ──────────────────

bool ModelPricing::match_pattern(const std::string &pattern,
                                 const std::string &model) {
    return glob_match(pattern, model);
}

// ── load ─────────────────────────────────────────────────────────────────

void ModelPricing::load(Database &db) {
    auto rows = db.get_all_pricing();
    entries_.clear();
    entries_.reserve(rows.size());
    for (const auto &r : rows) {
        entries_.push_back({r.model_pattern, r.input_price, r.output_price});
    }
}

// ── estimate_cost ────────────────────────────────────────────────────────

double ModelPricing::estimate_cost(const std::string &model,
                                   int prompt_tokens,
                                   int completion_tokens) const {
    const Entry *match = nullptr;

    for (const auto &e : entries_) {
        if (match_pattern(e.pattern, model)) {
            match = &e;
            break;  // first match wins
        }
    }

    double input_price = match ? match->input_price : 0.001;
    double output_price = match ? match->output_price : 0.002;

    // Prices are per 1M tokens
    double cost = (prompt_tokens / 1000000.0) * input_price +
                  (completion_tokens / 1000000.0) * output_price;
    return cost;
}
