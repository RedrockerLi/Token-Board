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
            "WHERE account_id=?1 AND charge_type='recurring' AND ends_at IS NULL) "
            "THEN 0 ELSE ?8 END,?9,?10,?11,COALESCE(?13,0),COALESCE(?14,0),"
            "COALESCE(?15,0),COALESCE(?16,0),COALESCE(?17,0),?18,?19,"
            "strftime('%Y-%m-%dT%H:%M:%fZ',?20,'unixepoch'),?22,?23,'pending',?1,"
            "CASE WHEN EXISTS(SELECT 1 FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND ends_at IS NULL) THEN COALESCE("
            "(SELECT uuid FROM upstream_credentials WHERE runtime_id=?12),"
            "(SELECT 'contract:' || uuid FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND ends_at IS NULL ORDER BY id DESC LIMIT 1)) END,"
            "(SELECT uuid FROM billing_contracts WHERE account_id=?1 AND charge_type='recurring' "
            "AND ends_at IS NULL ORDER BY id DESC LIMIT 1),"
            "(SELECT billing_anchor_day FROM billing_contracts WHERE account_id=?1 "
            "AND charge_type='recurring' AND ends_at IS NULL ORDER BY id DESC LIMIT 1))",
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
            "SELECT u.base_url,s.secret_value,u.api_format,u.auth_scheme "
            "FROM upstream_credentials c JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
            "JOIN upstreams u ON u.id=c.upstream_id JOIN accounts a ON a.id=u.account_id "
            "WHERE c.runtime_id=?1 AND a.account_kind='proxy' "
            "AND c.enabled=1 "
            "AND (c.ends_at IS NULL OR c.ends_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
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
    FINALIZE(stmt_insert_log_);
    FINALIZE(stmt_find_log_event_);
    FINALIZE(stmt_insert_attempt_);
    FINALIZE(stmt_lookup_probe_target_);
    FINALIZE(stmt_update_last_used_);
    FINALIZE(stmt_update_accounting_);
    #undef FINALIZE
}
