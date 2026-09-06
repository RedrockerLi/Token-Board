#include "database_internal.h"

bool Database::prepare_statements() {
    bool ok = true;
    #define PREPARE_ON(conn, sql, stmt) \
        do { \
            int _rc = sqlite3_prepare_v2(conn, sql, -1, &stmt, nullptr); \
            if (_rc != SQLITE_OK) { \
                TB_LOG_ERROR( "[DB] Prepare error: %s\n", sqlite3_errmsg(conn)); \
                if (stmt) sqlite3_finalize(stmt); \
                stmt = nullptr; \
                ok = false; \
            } \
        } while (0)

    {
        PREPARE_ON(read_db_,
            "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,'') "
            "FROM client_keys ck WHERE ck.key_value=?1 AND ck.enabled=1 "
            "AND (ck.deleted_at IS NULL OR ck.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            stmt_lookup_key_);
        PREPARE_ON(read_db_,
            "SELECT rs.id,rs.name,COALESCE(u.base_url,''),COALESCE(u.api_format,'openai'),"
            "COALESCE(u.endpoint_path,''),COALESCE(u.auth_scheme,'auto'),"
            "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,"
            "COALESCE(u.max_concurrency,0),CASE WHEN rs.enabled=1 THEN 0 ELSE 1 END "
            "FROM route_sets rs LEFT JOIN upstreams u ON u.account_id=rs.account_id "
            "AND u.enabled=1 LEFT JOIN accounts a ON a.id=rs.account_id "
            "WHERE rs.id=?1 AND (rs.account_id IS NULL OR "
            "COALESCE(a.account_kind,'proxy')='proxy') ORDER BY u.id LIMIT 1",
            stmt_get_account_);
        PREPARE_ON(read_db_,
            "SELECT ck.id,ck.key_value,ck.route_set_id,COALESCE(ck.label,''),"
            "rs.id,rs.name,COALESCE(u.base_url,''),COALESCE(u.api_format,'openai'),"
            "COALESCE(u.endpoint_path,''),COALESCE(u.auth_scheme,'auto'),"
            "CASE WHEN rs.account_id IS NULL THEN 1 ELSE 0 END,0,"
            "COALESCE(u.max_concurrency,0),0 "
            "FROM client_keys ck JOIN route_sets rs ON rs.id=ck.route_set_id "
            "LEFT JOIN upstreams u ON u.account_id=rs.account_id AND u.enabled=1 "
            "LEFT JOIN accounts a ON a.id=rs.account_id "
            "WHERE ck.key_value=?1 AND ck.enabled=1 AND rs.enabled=1 "
            "AND (rs.account_id IS NULL OR COALESCE(a.account_kind,'proxy')='proxy') "
            "AND (ck.deleted_at IS NULL OR ck.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ORDER BY u.id LIMIT 1",
            stmt_lookup_route_);
        PREPARE_ON(read_db_,
            "SELECT c.runtime_id,s.secret_value,c.position FROM upstream_credentials c "
            "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "JOIN upstreams u ON u.id=c.upstream_id JOIN accounts a ON a.id=u.account_id "
            "WHERE u.account_id=?1 AND a.account_kind='proxy' "
            "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ORDER BY c.position,c.runtime_id",
            stmt_get_upstream_keys_);
        PREPARE_ON(write_db_,
            "INSERT INTO request_log(event_id,source_kind,account_id,route_set_id,"
            "client_key_id,upstream_key_id,credential_uuid,model,prompt_tokens,completion_tokens,"
            "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,"
            "is_streaming,status_code,duration_ms,ttft_ms,generation_ms,output_tps,"
            "upstream_ttft_ms,upstream_duration_ms,attempt_count,fallback_count,"
            "requested_at,queue_ms,accounting_ms,pricing_status,account_identity_id,"
            "billing_unit_id,billing_contract_uuid,billing_anchor_day) "
            "VALUES(?21,'proxy',(SELECT id FROM accounts WHERE id=?1),"
            "(SELECT route_set_id FROM client_keys WHERE id=?2),"
            "(SELECT id FROM client_keys WHERE id=?2),?12,"
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?12),"
            "?3,?4,?5,?6,?7,?8,CASE WHEN EXISTS(SELECT 1 FROM billing_contracts "
            "WHERE account_id=?1 AND charge_type='recurring' AND valid_until IS NULL) "
            "THEN 0 ELSE ?8 END,?9,?10,?11,COALESCE(?13,0),COALESCE(?14,0),"
            "COALESCE(?15,0),COALESCE(?16,0),COALESCE(?17,0),?18,?19,"
            "strftime('%Y-%m-%dT%H:%M:%fZ',?20,'unixepoch'),?22,?23,'pending',?1,"
            "CASE WHEN EXISTS(SELECT 1 FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND valid_until IS NULL) THEN COALESCE("
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?12),"
            "(SELECT 'contract:' || uuid FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND valid_until IS NULL ORDER BY id DESC LIMIT 1)) END,"
            "(SELECT uuid FROM billing_contracts WHERE account_id=?1 AND charge_type='recurring' "
            "AND valid_until IS NULL ORDER BY id DESC LIMIT 1),"
            "(SELECT billing_anchor_day FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND valid_until IS NULL ORDER BY id DESC LIMIT 1))",
            stmt_insert_log_);
        PREPARE_ON(write_db_, "SELECT id FROM request_log WHERE event_id=?1",
                   stmt_find_log_event_);
        PREPARE_ON(write_db_,
            "INSERT INTO request_attempts(request_log_id,attempt_index,upstream_id,"
            "credential_uuid,account_id,upstream_key_id,status_code,duration_ms,ttft_ms,is_timeout,error,requested_at,"
            "dns_ms,connect_ms,tls_ms,lease_wait_ms,first_byte_ms,connection_reused) "
            "VALUES(?1,?2,COALESCE((SELECT id FROM upstreams WHERE id=?3),"
            "(SELECT id FROM upstreams WHERE account_id=?17 ORDER BY id LIMIT 1)),"
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?4),?17,?4,?5,?6,"
            "COALESCE(?7,0),?8,?9,strftime('%Y-%m-%dT%H:%M:%fZ',?10,'unixepoch'),"
            "?11,?12,?13,?14,?15,?16)",
            stmt_insert_attempt_);
        PREPARE_ON(read_db_,
            "SELECT model_pattern,u.account_id,COALESCE(target_model,model_pattern) "
            "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
            "JOIN accounts a ON a.id=u.account_id AND a.account_kind='proxy' "
            "WHERE route_set_id=?1 AND rr.enabled=1 ORDER BY priority,rr.id",
            stmt_get_aggregate_entries_);
        PREPARE_ON(read_db_,
            "SELECT id,model_pattern,input_price,output_price "
            "FROM pricing_rules ORDER BY priority,id",
            stmt_get_pricing_);
        PREPARE_ON(read_db_,
            "SELECT streaming_first_byte_timeout,streaming_idle_timeout,"
            "non_streaming_timeout FROM proxy_timeout_config WHERE endpoint_kind=?1",
            stmt_get_timeout_config_);
        PREPARE_ON(read_db_,
            "SELECT u.base_url,s.secret_value,u.api_format,u.auth_scheme "
            "FROM upstream_credentials c JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "JOIN upstreams u ON u.id=c.upstream_id JOIN accounts a ON a.id=u.account_id "
            "WHERE c.runtime_id=?1 AND a.account_kind='proxy' "
            "AND (c.disabled_at IS NULL OR c.disabled_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND (c.deleted_at IS NULL OR c.deleted_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "AND u.enabled=1",
            stmt_lookup_probe_target_);
        PREPARE_ON(write_db_,
            "UPDATE client_keys SET last_used_at=strftime('%Y-%m-%dT%H:%M:%fZ',?2,'unixepoch') "
            "WHERE id=?1 AND (last_used_at IS NULL OR "
            "last_used_at<strftime('%Y-%m-%dT%H:%M:%fZ',?2,'unixepoch'))",
            stmt_update_last_used_);
        PREPARE_ON(write_db_,
            "UPDATE request_log SET accounting_ms=?1 WHERE event_id=?2",
            stmt_update_accounting_);
        return ok;
    }
    #undef PREPARE_ON
    return ok;
}

void Database::finalize_statements() {
    #define FINALIZE(s) do { if (s) { sqlite3_finalize(s); s = nullptr; } } while (0)
    FINALIZE(stmt_lookup_key_);
    FINALIZE(stmt_get_account_);
    FINALIZE(stmt_lookup_route_);
    FINALIZE(stmt_get_upstream_keys_);
    FINALIZE(stmt_insert_log_);
    FINALIZE(stmt_find_log_event_);
    FINALIZE(stmt_insert_attempt_);
    FINALIZE(stmt_get_aggregate_entries_);
    FINALIZE(stmt_get_pricing_);
    FINALIZE(stmt_get_timeout_config_);
    FINALIZE(stmt_lookup_probe_target_);
    FINALIZE(stmt_update_last_used_);
    FINALIZE(stmt_update_accounting_);
    #undef FINALIZE
}

// ── lookup_local_key ─────────────────────────────────────────────────────
