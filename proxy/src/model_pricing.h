#pragma once

#include <string>
#include <vector>

/// Shell-style glob match: supports * (any chars) and ? (single char).
/// Case-insensitive. Returns true if pattern matches text.
bool glob_match(const std::string &pattern, const std::string &text);

class Database;

/// In-memory model-pricing table loaded from SQLite.
///
/// Supports glob-style pattern matching so a single entry like "deepseek-*"
/// covers all DeepSeek variants.  Patterns are checked in insertion order;
/// the first match wins.  A catch-all "*" entry provides the default price.
class ModelPricing {
public:
    /// (Re)load pricing entries from the database.
    void load(Database &db);
    void reload(Database &db) { load(db); }

    /// Estimate cost in CNY given token counts.  Falls back to a hard-coded
    /// default if no pattern matches (shouldn't happen if a "*" catch-all
    /// exists in the DB).
    double estimate_cost(const std::string &model, int prompt_tokens,
                         int completion_tokens) const;

private:
    struct Entry {
        std::string pattern;
        double input_price;   // per 1K tokens
        double output_price;  // per 1K tokens
    };
    std::vector<Entry> entries_;

    /// Simple glob match (supports * and ? wildcards).
    static bool match_pattern(const std::string &pattern,
                              const std::string &model);
};
