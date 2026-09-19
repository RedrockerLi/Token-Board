"""Import durable C++ usage frames into a V1 transition shadow."""

from datetime import datetime, timezone

from transition_common import LEGACY_CREDENTIAL_UUID, stable_uuid, utc_timestamp


def append_spool_requests(old, new, spool_records, credential_map, route_ids,
                          contract_ids, legacy_account_id, source_tz):
    rows = [dict(row) for row in old.execute("SELECT * FROM request_log ORDER BY id")]
    existing = {row.get("event_id") for row in rows}
    imported = []
    for record in spool_records or []:
        if record.get("event_id") in existing:
            continue
        record["_from_spool"] = True
        rows.append(record)
    for row in rows:
        key_id = row.get("upstream_key_id")
        key_uuid = credential_map.get(key_id)
        if key_id is not None and key_uuid is None:
            key_uuid = LEGACY_CREDENTIAL_UUID
        client = row.get("local_key_id")
        route = None
        if client is not None:
            found = old.execute("SELECT account_id FROM local_keys WHERE id=?", (client,)).fetchone()
            route = found[0] if found and found[0] in route_ids else None
            if route is None:
                client = None
        source_account = int(row.get("account_id") or 0)
        account_id = source_account if source_account in contract_ids else legacy_account_id
        charge = "metered" if account_id == legacy_account_id else old.execute(
            "SELECT account_type FROM upstream_accounts WHERE id=?", (account_id,)).fetchone()[0]
        source_cost = float(row.get("api_cost", row.get("cost", 0.0)) or 0.0)
        billed = source_cost if charge == "api" else 0.0
        event_id = row.get("event_id") or stable_uuid("request", row.get("id"))
        requested_at = row.get("requested_at")
        if row.get("_from_spool"):
            requested_at = datetime.fromtimestamp(
                int(row.get("requested_at_unix", 0)), timezone.utc).isoformat()
        fields = ("event_id,source_kind,account_id,route_set_id,client_key_id,"
                  "upstream_key_id,credential_uuid,model,prompt_tokens,completion_tokens,"
                  "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,is_streaming,"
                  "status_code,duration_ms,ttft_ms,generation_ms,output_tps,upstream_ttft_ms,"
                  "upstream_duration_ms,attempt_count,fallback_count,requested_at")
        values = (event_id, "proxy", account_id, route, client, key_id, key_uuid,
                  row.get("model", ""), row.get("prompt_tokens", 0) or 0,
                  row.get("completion_tokens", 0) or 0,
                  row.get("cache_read_tokens", 0) or 0,
                  row.get("total_tokens", 0) or 0, source_cost, billed,
                  row.get("is_streaming", 0) or 0, row.get("status_code", 0),
                  row.get("duration_ms", 0) or 0, row.get("ttft_ms", 0) or 0,
                  row.get("generation_ms", 0) or 0, row.get("output_tps", 0) or 0,
                  row.get("upstream_ttft_ms", 0) or 0,
                  row.get("upstream_duration_ms", 0) or 0,
                  row.get("attempt_count", 0) or 0, row.get("fallback_count", 0) or 0,
                  utc_timestamp(requested_at, source_tz))
        placeholders = ",".join("?" for _ in values)
        if row.get("_from_spool"):
            cursor = new.execute("INSERT INTO request_log(" + fields + ") VALUES(" +
                                 placeholders + ")", values)
            imported.append((cursor.lastrowid, row))
        else:
            new.execute("INSERT INTO request_log(id," + fields + ") VALUES(?," +
                        placeholders + ")", (row["id"], *values))
    return imported


def append_spool_attempts(new, imported, credential_map, routable_ids,
                          legacy_upstream_id):
    for request_id, record in imported:
        requested_at = datetime.fromtimestamp(
            int(record.get("requested_at_unix", 0)), timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        for index, attempt in enumerate(record.get("attempts", []), 1):
            account_id = int(attempt.get("account_id") or 0)
            key_id = int(attempt.get("upstream_key_id") or 0)
            upstream = account_id if account_id in routable_ids else legacy_upstream_id
            new.execute(
                "INSERT INTO request_attempts(request_log_id,attempt_index,upstream_id,"
                "credential_uuid,account_id,upstream_key_id,status_code,duration_ms,ttft_ms,"
                "is_timeout,error,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, index, upstream,
                 credential_map.get(key_id, LEGACY_CREDENTIAL_UUID), account_id, key_id,
                 int(attempt.get("status_code") or 0),
                 int(attempt.get("duration_ms") or 0), int(attempt.get("ttft_ms") or 0),
                 1 if attempt.get("is_timeout") else 0, attempt.get("error", ""),
                 requested_at),
            )
