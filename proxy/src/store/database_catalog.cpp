#include "database_internal.h"

std::vector<Database::AggregateEntry>
Database::resolve_aggregate(int account_id, const std::string &model) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    std::vector<AggregateEntry> result;
    sqlite3_reset(stmt_get_aggregate_entries_);
    sqlite3_bind_int(stmt_get_aggregate_entries_, 1, account_id);
    while (sqlite3_step(stmt_get_aggregate_entries_) == SQLITE_ROW) {
        std::string pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_aggregate_entries_, 0));
        // Exact (case-sensitive) match: entries are concrete model names —
        // the aggregate's model catalog, not wildcard patterns.  One model
        // may map to several upstream accounts (priority = sort_order, id,
        // already applied by the query); collect ALL matches so the caller
        // can try them in order, skipping busy / cooling-down accounts.
        if (pattern == model) {
            AggregateEntry e;
            e.pattern = pattern;
            e.upstream_account_id = sqlite3_column_int(stmt_get_aggregate_entries_, 1);
            e.upstream_model = reinterpret_cast<const char *>(
                sqlite3_column_text(stmt_get_aggregate_entries_, 2));
            result.push_back(std::move(e));
        }
    }
    sqlite3_reset(stmt_get_aggregate_entries_);
    return result;
}

// ── get_aggregate_model_patterns ─────────────────────────────────────────

std::vector<std::string>
Database::get_aggregate_model_patterns(int account_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    std::vector<std::string> result;
    sqlite3_reset(stmt_get_aggregate_entries_);
    sqlite3_bind_int(stmt_get_aggregate_entries_, 1, account_id);
    while (sqlite3_step(stmt_get_aggregate_entries_) == SQLITE_ROW) {
        result.emplace_back(reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_aggregate_entries_, 0)));
    }
    sqlite3_reset(stmt_get_aggregate_entries_);
    return result;
}

// ── get_all_pricing ─────────────────────────────────────────────────────

std::vector<Database::PricingEntry> Database::get_all_pricing() {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    std::vector<PricingEntry> result;
    sqlite3_reset(stmt_get_pricing_);

    while (sqlite3_step(stmt_get_pricing_) == SQLITE_ROW) {
        PricingEntry e;
        e.id = sqlite3_column_int(stmt_get_pricing_, 0);
        e.model_pattern = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_pricing_, 1));
        e.input_price = sqlite3_column_double(stmt_get_pricing_, 2);
        e.output_price = sqlite3_column_double(stmt_get_pricing_, 3);
        result.push_back(e);
    }
    sqlite3_reset(stmt_get_pricing_);
    return result;
}

// ── get_timeout_config ──────────────────────────────────────────────────

Database::TimeoutConfig Database::get_timeout_config(const std::string &app_type) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    TimeoutConfig tc;
    sqlite3_reset(stmt_get_timeout_config_);
    sqlite3_bind_text(stmt_get_timeout_config_, 1,
                      app_type.c_str(), app_type.size(), SQLITE_STATIC);

    if (sqlite3_step(stmt_get_timeout_config_) == SQLITE_ROW) {
        tc.streaming_first_byte_timeout = sqlite3_column_int(stmt_get_timeout_config_, 0);
        tc.streaming_idle_timeout = sqlite3_column_int(stmt_get_timeout_config_, 1);
        tc.non_streaming_timeout = sqlite3_column_int(stmt_get_timeout_config_, 2);
    }
    // Missing row (or migration not applied on an ancient DB) → struct defaults.

    sqlite3_reset(stmt_get_timeout_config_);
    return tc;
}
