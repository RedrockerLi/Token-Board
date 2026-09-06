#include "database_internal.h"

std::optional<Database::KeyInfo> Database::lookup_local_key(
    const std::string &key_value) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_lookup_key_);
    sqlite3_bind_text(stmt_lookup_key_, 1,
                      key_value.c_str(), key_value.size(), SQLITE_STATIC);

    std::optional<KeyInfo> result;
    if (sqlite3_step(stmt_lookup_key_) == SQLITE_ROW) {
        KeyInfo info;
        info.id = sqlite3_column_int(stmt_lookup_key_, 0);
        info.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_key_, 1));
        info.account_id = sqlite3_column_int(stmt_lookup_key_, 2);
        info.label = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_key_, 3));
        result = std::move(info);
    }
    sqlite3_reset(stmt_lookup_key_);
    return result;
}

// ── get_account ──────────────────────────────────────────────────────────

std::optional<Database::AccountInfo> Database::get_account(int account_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_get_account_);
    sqlite3_bind_int(stmt_get_account_, 1, account_id);

    std::optional<AccountInfo> result;
    if (sqlite3_step(stmt_get_account_) == SQLITE_ROW) {
        AccountInfo info;
        info.id = sqlite3_column_int(stmt_get_account_, 0);
        info.name = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 1));
        info.base_url = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 2));
        info.api_format = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 3));
        if (info.api_format.empty()) info.api_format = "openai";
        info.endpoint_path = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 4));
        info.auth_header = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_account_, 5));
        if (info.auth_header.empty()) info.auth_header = "bearer";
        info.is_aggregate = sqlite3_column_int(stmt_get_account_, 6) != 0;
        info.extended_usage_limit_cooldown =
            sqlite3_column_int(stmt_get_account_, 7) != 0;
        info.max_concurrency = sqlite3_column_int(stmt_get_account_, 8);
        info.deleted = sqlite3_column_int(stmt_get_account_, 9) != 0;
        result = std::move(info);
    }
    sqlite3_reset(stmt_get_account_);
    return result;
}

std::optional<Database::RouteInfo> Database::lookup_route(
    const std::string &key_value) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_reset(stmt_lookup_route_);
    sqlite3_bind_text(stmt_lookup_route_, 1, key_value.c_str(),
                      key_value.size(), SQLITE_STATIC);
    std::optional<RouteInfo> result;
    if (sqlite3_step(stmt_lookup_route_) == SQLITE_ROW) {
        RouteInfo route;
        route.key.id = sqlite3_column_int(stmt_lookup_route_, 0);
        route.key.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_route_, 1));
        route.key.account_id = sqlite3_column_int(stmt_lookup_route_, 2);
        route.key.label = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_route_, 3));
        auto &a = route.account;
        a.id = sqlite3_column_int(stmt_lookup_route_, 4);
        a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 5));
        a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 6));
        a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 7));
        a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 8));
        a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt_lookup_route_, 9));
        a.is_aggregate = sqlite3_column_int(stmt_lookup_route_, 10) != 0;
        a.extended_usage_limit_cooldown =
            sqlite3_column_int(stmt_lookup_route_, 11) != 0;
        a.max_concurrency = sqlite3_column_int(stmt_lookup_route_, 12);
        a.deleted = sqlite3_column_int(stmt_lookup_route_, 13) != 0;
        result = std::move(route);
    }
    sqlite3_reset(stmt_lookup_route_);
    return result;
}

std::uint64_t Database::routing_config_generation() {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_stmt *stmt = nullptr;
    std::uint64_t generation = 0;
    const char *sql = "SELECT generation FROM config_state WHERE id=1";
    if (sqlite3_prepare_v2(read_db_, sql, -1, &stmt,
                           nullptr) == SQLITE_OK &&
        sqlite3_step(stmt) == SQLITE_ROW)
        generation = static_cast<std::uint64_t>(sqlite3_column_int64(stmt, 0));
    sqlite3_finalize(stmt);
    return generation;
}

bool Database::load_routing_config(RoutingConfig &config) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    RoutingConfig next;
    char *error = nullptr;
    if (sqlite3_exec(read_db_, "BEGIN", nullptr, nullptr, &error) != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] routing snapshot begin failed: %s\n",
                error ? error : sqlite3_errmsg(read_db_));
        if (error) sqlite3_free(error);
        return false;
    }
    ReadTransactionGuard rollback(read_db_);

    sqlite3_stmt *stmt = nullptr;
    const char *generation_sql = "SELECT generation FROM config_state WHERE id=1";
    if (sqlite3_prepare_v2(read_db_, generation_sql, -1, &stmt,
                           nullptr) == SQLITE_OK &&
        sqlite3_step(stmt) == SQLITE_ROW)
        next.generation = static_cast<std::uint64_t>(
            sqlite3_column_int64(stmt, 0));
    sqlite3_finalize(stmt);

    const std::string routes_sql =
        "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,''),"
        "rs.id,rs.name,'','openai','','auto',"
        "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,0,0 "
        "FROM client_keys ck JOIN route_sets rs ON rs.id=ck.route_set_id "
        "LEFT JOIN accounts a ON a.id=rs.account_id "
        "WHERE ck.enabled=1 AND rs.enabled=1 "
        "AND (rs.account_id IS NULL OR COALESCE(a.account_kind,'proxy')='proxy') "
        "ORDER BY ck.id";
    if (sqlite3_prepare_v2(read_db_, routes_sql.c_str(), -1, &stmt,
                           nullptr) != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] routing snapshot routes failed: %s\n",
                sqlite3_errmsg(read_db_));
        return false;
    }
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        RouteInfo route;
        route.key.id = sqlite3_column_int(stmt, 0);
        route.key.key_value = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 1));
        route.key.account_id = sqlite3_column_int(stmt, 2);
        route.key.label = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 3));
        auto &a = route.account;
        a.id = sqlite3_column_int(stmt, 4);
        a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 5));
        a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 6));
        a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 7));
        a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 8));
        a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 9));
        a.is_aggregate = sqlite3_column_int(stmt, 10) != 0;
        a.extended_usage_limit_cooldown = sqlite3_column_int(stmt, 11) != 0;
        a.max_concurrency = sqlite3_column_int(stmt, 12);
        a.deleted = false;
        next.routes.push_back(std::move(route));
    }
    sqlite3_finalize(stmt);
    stmt = nullptr;

    const std::string rules_sql =
        "SELECT rr.route_set_id,rr.model_pattern,rr.target_model,rr.priority,"
        "a.id,u.id,a.name,u.base_url,u.api_format,COALESCE(u.endpoint_path,''),"
        "COALESCE(u.auth_scheme,'auto'),"
        "CASE WHEN json_extract(COALESCE(bc.cooldown_policy_json,'{}'),'$.kind')="
        "'subscription_5h' THEN 1 ELSE 0 END,"
        "u.max_concurrency,c.runtime_id,s.secret_value,c.position "
        "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
        "JOIN accounts a ON a.id=u.account_id "
        "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
        "AND bc.ends_at IS NULL "
        "LEFT JOIN upstream_credentials c ON c.upstream_id=u.id "
        "AND c.enabled=1 "
        "AND (c.ends_at IS NULL OR c.ends_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
        "WHERE rr.enabled=1 AND u.enabled=1 "
        "AND a.account_kind='proxy' "
        "ORDER BY rr.route_set_id,rr.priority,rr.id,c.position,c.runtime_id";
    if (sqlite3_prepare_v2(read_db_, rules_sql.c_str(), -1, &stmt,
                           nullptr) != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] routing snapshot rules failed: %s\n",
                sqlite3_errmsg(read_db_));
        return false;
    }
    int last_route = -1;
    int last_group = -1;
    int last_target = -1;
    std::string last_pattern;
    std::string last_target_model;
    std::unordered_map<int, std::shared_ptr<const AccountInfo>> account_refs;
    std::unordered_map<int, std::shared_ptr<const KeySlot>> key_refs;
    std::unordered_map<std::string, std::shared_ptr<const std::string>> model_refs;
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        const int route_id = sqlite3_column_int(stmt, 0);
        const char *pattern_text = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 1));
        const std::string pattern = pattern_text ? pattern_text : "*";
        const int group = sqlite3_column_int(stmt, 3);
        const int target_id = sqlite3_column_int(stmt, 4);
        const int upstream_id = sqlite3_column_int(stmt, 5);
        const unsigned char *target_model = sqlite3_column_text(stmt, 2);
        const std::string target_model_value = target_model
            ? reinterpret_cast<const char *>(target_model) : "";
        if (next.rules.empty() || route_id != last_route || group != last_group ||
            upstream_id != last_target || pattern != last_pattern ||
            target_model_value != last_target_model) {
            RoutingRule rule;
            rule.route_set_id = route_id;
            rule.model_pattern = pattern;
            rule.target.priority_group = group;
            AccountInfo account;
            auto &a = account;
            a.id = target_id;
            a.name = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 6));
            a.base_url = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 7));
            a.api_format = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 8));
            a.endpoint_path = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 9));
            a.auth_header = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 10));
            a.upstream_id = upstream_id;
            a.extended_usage_limit_cooldown = sqlite3_column_int(stmt, 11) != 0;
            a.max_concurrency = sqlite3_column_int(stmt, 12);
            a.is_aggregate = false;
            a.deleted = false;
            auto account_it = account_refs.try_emplace(
                upstream_id, std::make_shared<const AccountInfo>(account)).first;
            rule.target.account_ref = account_it->second;
            // NULL target_model means "preserve the client model". Keep a
            // null reference for that common ordinary-account rule instead
            // of manufacturing an empty model string.
            if (!target_model_value.empty()) {
                auto model_it = model_refs.try_emplace(
                    target_model_value,
                    std::make_shared<const std::string>(target_model_value)).first;
                rule.target.upstream_model_ref = model_it->second;
            }
            next.rules.push_back(std::move(rule));
            last_route = route_id;
            last_group = group;
            last_target = upstream_id;
            last_pattern = pattern;
            last_target_model = target_model_value;
        }
        // Cloud configuration carries credential metadata but deliberately
        // omits upstream_secrets. Such credentials remain visible for sync
        // and UI, but are not routable on this node until confirmed locally.
        if (sqlite3_column_type(stmt, 13) != SQLITE_NULL &&
            sqlite3_column_type(stmt, 14) != SQLITE_NULL) {
            KeySlot key;
            key.id = sqlite3_column_int(stmt, 13);
            key.key_value = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 14));
            key.position = sqlite3_column_int(stmt, 15);
            auto key_it = key_refs.try_emplace(
                key.id, std::make_shared<const KeySlot>(key)).first;
            next.rules.back().target.key_refs.push_back(key_it->second);
        }
    }
    sqlite3_finalize(stmt);
    stmt = nullptr;
    const char *timeouts_sql =
        "SELECT endpoint_kind,streaming_first_byte_timeout,streaming_idle_timeout,"
        "non_streaming_timeout FROM proxy_timeout_config";
    if (sqlite3_prepare_v2(read_db_, timeouts_sql, -1, &stmt, nullptr) != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] routing snapshot timeouts failed: %s\n",
                sqlite3_errmsg(read_db_));
        return false;
    }
    while (sqlite3_step(stmt) == SQLITE_ROW) {
        const char *name = reinterpret_cast<const char *>(sqlite3_column_text(stmt, 0));
        TimeoutConfig timeout;
        timeout.streaming_first_byte_timeout = sqlite3_column_int(stmt, 1);
        timeout.streaming_idle_timeout = sqlite3_column_int(stmt, 2);
        timeout.non_streaming_timeout = sqlite3_column_int(stmt, 3);
        if (name) {
            next.timeouts.emplace(name, timeout);
        }
    }
    sqlite3_finalize(stmt);
    if (sqlite3_exec(read_db_, "COMMIT", nullptr, nullptr, &error) != SQLITE_OK) {
        TB_LOG_ERROR( "[DB] routing snapshot commit failed: %s\n",
                error ? error : sqlite3_errmsg(read_db_));
        if (error) sqlite3_free(error);
        return false;
    }
    rollback.release();
    config = std::move(next);
    return true;
}

// ── get_upstream_keys ────────────────────────────────────────────────────

std::vector<Database::KeySlot> Database::get_upstream_keys(int account_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);

    sqlite3_reset(stmt_get_upstream_keys_);
    sqlite3_bind_int(stmt_get_upstream_keys_, 1, account_id);

    std::vector<KeySlot> result;
    while (sqlite3_step(stmt_get_upstream_keys_) == SQLITE_ROW) {
        KeySlot k;
        k.id = sqlite3_column_int(stmt_get_upstream_keys_, 0);
        k.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_get_upstream_keys_, 1));
        k.position = sqlite3_column_int(stmt_get_upstream_keys_, 2);
        result.push_back(std::move(k));
    }
    sqlite3_reset(stmt_get_upstream_keys_);
    return result;
}

// ── lookup_probe_target ──────────────────────────────────────────────────

std::optional<Database::ProbeTarget> Database::lookup_probe_target(
    int key_slot_id) {
    std::shared_lock<std::shared_mutex> lifecycle(lifecycle_mutex_);
    std::lock_guard<std::mutex> lock(read_mutex_);
    sqlite3_reset(stmt_lookup_probe_target_);
    sqlite3_bind_int(stmt_lookup_probe_target_, 1, key_slot_id);
    ProbeTarget t;
    if (sqlite3_step(stmt_lookup_probe_target_) == SQLITE_ROW) {
        t.base_url = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 0));
        t.key_value = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 1));
        t.api_format = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 2));
        t.auth_header = reinterpret_cast<const char *>(
            sqlite3_column_text(stmt_lookup_probe_target_, 3));
        t.valid = !t.base_url.empty() && !t.key_value.empty();
    }
    sqlite3_reset(stmt_lookup_probe_target_);
    if (!t.valid) return std::nullopt;
    return t;
}

// ── log_request ──────────────────────────────────────────────────────────
