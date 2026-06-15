#include "model_pricing.h"
#include "db.h"

#include <algorithm>
#include <cstring>

// ── match_pattern — simple glob with * and ? ─────────────────────────────

bool ModelPricing::match_pattern(const std::string &pattern,
                                 const std::string &model) {
    // Simple recursive glob matcher (no backtracking for **).
    // Handles the patterns we use: literal prefixes with * suffix, full *, etc.

    size_t pi = 0, mi = 0;
    size_t star_pos = std::string::npos;
    size_t match_pos = 0;

    while (mi < model.size()) {
        if (pi < pattern.size() &&
            (pattern[pi] == '?' ||
             tolower(pattern[pi]) == tolower(model[mi]))) {
            ++pi;
            ++mi;
        } else if (pi < pattern.size() && pattern[pi] == '*') {
            star_pos = pi;
            match_pos = mi;
            ++pi;
        } else if (star_pos != std::string::npos) {
            pi = star_pos + 1;
            match_pos++;
            mi = match_pos;
        } else {
            return false;
        }
    }

    // Consume trailing stars
    while (pi < pattern.size() && pattern[pi] == '*')
        ++pi;

    return pi == pattern.size();
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
