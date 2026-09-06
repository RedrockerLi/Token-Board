"""ProxyDatabase methods for ProxyAccountWriteMixin."""

from app.core.time import billing_period, utc_now
from app.db.proxy.common import (
    ACCOUNT_TYPES, _parse_iso_date, _subscription_date, is_subscription, json,
    spec, sqlite3, uuid,
)


class ProxyAccountWriteMixin:
    def create_account(self, data: dict) -> int:
        account_type = data.get("account_type", "api")
        if account_type not in ACCOUNT_TYPES:
            raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
        currency = data.get("currency", "CNY")
        if currency not in ("CNY", "USD"):
            raise ValueError("币种必须是 CNY / USD")
        keys = self._normalize_keys(data)
        type_spec = spec(account_type)
        raw_valid_from = data.get("valid_from")
        valid_from = (_parse_iso_date(_subscription_date(raw_valid_from))
                      if raw_valid_from not in (None, "") else None)
        conn = self._connect()
        try:
            shared_id = self._next_shared_id(conn)
            now = utc_now()
            start_date = (valid_from or now.date()).isoformat()
            if is_subscription(account_type):
                contract_start = _subscription_date(data.get("valid_from"))
            elif valid_from:
                contract_start = f"{start_date}T00:00:00Z"
            else:
                contract_start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            rate_start = contract_start
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,valid_from) VALUES(?,?,?,?)",
                (shared_id, str(uuid.uuid4()), data["name"], start_date),
            )
            conn.execute(
                "INSERT OR IGNORE INTO account_identities"
                "(id,uuid,name,account_kind,created_at,updated_at) "
                "SELECT id,uuid,name,'proxy',created_at,updated_at "
                "FROM accounts WHERE id=?", (shared_id,)
            )
            contract_id = conn.execute(
                "INSERT INTO billing_contracts"
                "(uuid,account_id,charge_type,billing_scope,currency,billing_anchor_day,"
                "cooldown_policy_json,valid_from) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), shared_id,
                 "recurring" if is_subscription(account_type) else "metered",
                 "credential" if type_spec.subscription_unit == "per_key" else "account",
                 currency, (valid_from or now.date()).day,
                 json.dumps({"kind": type_spec.cooldown or "none"}), contract_start),
            ).lastrowid
            if is_subscription(account_type):
                conn.execute(
                    "INSERT INTO billing_rate_events"
                    "(contract_id,recurring_price,effective_at,effective_rule) VALUES(?,?,?,'next_period')",
                    (contract_id, float(data.get("monthly_price", 0) or 0), rate_start),
                )
            upstream_id = conn.execute(
                "INSERT INTO upstreams"
                "(account_id,name,base_url,api_format,auth_scheme,endpoint_path,max_concurrency) "
                "VALUES(?,?,?,?,?,?,?)",
                (shared_id, data["name"], data.get("base_url", ""),
                 data.get("api_format", "openai"), data.get("auth_header", "bearer"),
                 data.get("endpoint_path", ""), int(data.get("max_concurrency") or 0)),
            ).lastrowid
            conn.execute(
                "INSERT INTO route_sets(id,uuid,account_id,name) VALUES(?,?,?,?)",
                (shared_id, str(uuid.uuid4()), shared_id, data["name"]),
            )
            conn.execute(
                "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
                "VALUES(?,'*',0,?)", (shared_id, upstream_id),
            )
            if keys and type_spec.holds_keys:
                self._set_upstream_keys(
                    conn, shared_id, [], keys,
                    new_valid_froms=data.get("new_valid_froms"),
                    account_type=account_type)
            # Freeze at creation only when the configured start is the
            # current billing boundary. Backdated subscriptions are caught up
            # by the period-start worker; creation must not fabricate a row
            # for an old period that tests/operators may still be importing.
            creation_boundary = billing_period(
                now, (valid_from or now.date()).day).start.date()
            if is_subscription(account_type) and (
                    valid_from is None or creation_boundary == valid_from):
                from app.db.proxy.billing import materialize_period_charges_conn
                materialize_period_charges_conn(conn, now, current_only=True)
            conn.commit()
            return shared_id
        finally:
            conn.close()

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            return self._update_account_v1(conn, account_id, data)
        finally:
            conn.close()

    def _update_account_v1(self, conn: sqlite3.Connection, external_id: int,
                           data: dict) -> bool:
        if "price_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        route = self._v1_route_account(conn, external_id)
        real_id = route["account_id"] if route and route["account_id"] is not None else external_id
        original = conn.execute(
            "SELECT a.*,bc.id contract_id,bc.charge_type,bc.billing_scope,bc.currency,"
            "bc.cooldown_policy_json,"
            "bc.valid_from contract_valid_from,"
            "(SELECT recurring_price FROM billing_rate_events WHERE contract_id=bc.id "
            "ORDER BY effective_at DESC,id DESC LIMIT 1) current_price FROM accounts a "
            "LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.ends_at IS NULL "
            "WHERE a.id=? AND a.account_kind='proxy'",
            (real_id,),
        ).fetchone()
        if original is None:
            return False
        original_type = ("plan" if original["charge_type"] == "recurring" else "api")
        final_type = data.get("account_type", original_type)
        if final_type not in ACCOUNT_TYPES:
            raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
        final_spec = spec(final_type)
        currency = data.get("currency", original["currency"] or "CNY")
        if currency not in ("CNY", "USD"):
            raise ValueError("币种必须是 CNY / USD")
        name = data.get("name", original["name"])
        if "valid_from" in data:
            raw_start = data["valid_from"]
            parsed_start = (_parse_iso_date(_subscription_date(raw_start))
                            if raw_start not in (None, "") else None)
            start = parsed_start.isoformat() if parsed_start else None
        else:
            start = original["valid_from"]
        if is_subscription(final_type):
            contract_start = (_subscription_date(data.get("valid_from"))
                              if "valid_from" in data else
                              (start or original["contract_valid_from"]))
        else:
            contract_start = (f"{start}T00:00:00Z" if start
                              else original["contract_valid_from"])
        anchor_day = None
        if "valid_from" in data and parsed_start is not None:
            anchor_day = parsed_start.day
        conn.execute(
            "UPDATE accounts SET name=?,valid_from=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?", (name, start, real_id),
        )
        conn.execute(
            "UPDATE account_identities SET name=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?", (name, real_id),
        )
        charge = "recurring" if is_subscription(final_type) else "metered"
        scope = "credential" if final_spec.subscription_unit == "per_key" else "account"
        conn.execute(
            "UPDATE billing_contracts SET charge_type=?,billing_scope=?,currency=?,"
            "cooldown_policy_json=?,valid_from=?,"
            "billing_anchor_day=COALESCE(?,billing_anchor_day) WHERE id=?",
            (charge, scope, currency,
             json.dumps({"kind": final_spec.cooldown or "none"}),
             contract_start, anchor_day, original["contract_id"]),
        )
        upstream = conn.execute(
            "SELECT id FROM upstreams WHERE account_id=? ORDER BY id LIMIT 1", (real_id,)
        ).fetchone()
        if final_spec.routable:
            if upstream is None:
                upstream_id = conn.execute(
                    "INSERT INTO upstreams"
                    "(account_id,name,base_url,api_format,auth_scheme,endpoint_path,max_concurrency) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (real_id, name, data.get("base_url", ""), data.get("api_format", "openai"),
                     data.get("auth_header", "bearer"), data.get("endpoint_path", ""),
                     int(data.get("max_concurrency") or 0)),
                ).lastrowid
            else:
                upstream_id = upstream["id"]
                conn.execute(
                    "UPDATE upstreams SET name=?,base_url=COALESCE(?,base_url),"
                    "api_format=COALESCE(?,api_format),auth_scheme=COALESCE(?,auth_scheme),"
                    "endpoint_path=COALESCE(?,endpoint_path),max_concurrency=COALESCE(?,max_concurrency),"
                    "enabled=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                    (name, data.get("base_url"), data.get("api_format"),
                     data.get("auth_header"), data.get("endpoint_path"),
                     data.get("max_concurrency"), upstream_id),
                )
            route_row = conn.execute("SELECT id FROM route_sets WHERE id=?", (external_id,)).fetchone()
            if route_row is None:
                conn.execute(
                    "INSERT INTO route_sets(id,uuid,account_id,name) VALUES(?,?,?,?)",
                    (external_id, str(uuid.uuid4()), real_id, name),
                )
                conn.execute(
                    "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
                    "VALUES(?,'*',0,?)", (external_id, upstream_id),
                )
            else:
                conn.execute("UPDATE route_sets SET name=?,enabled=1 WHERE id=?", (name, external_id))
            if data.get("keys_edited") and final_spec.holds_keys:
                keep_ids = [int(value) for value in (data.get("keep_key_ids") or [])
                            if str(value).isdigit()]
                self._set_upstream_keys(
                    conn, external_id, keep_ids, self._normalize_keys(data),
                    keep_valid_froms=data.get("keep_valid_froms"),
                    new_valid_froms=data.get("new_valid_froms"), account_type=final_type)
        if is_subscription(final_type) and ("monthly_price" in data or
                                             original_type != final_type):
            conn.execute(
                "INSERT INTO billing_rate_events"
                "(contract_id,recurring_price,effective_at,effective_rule) VALUES(?,?,?,?)",
                (original["contract_id"], float(data.get("monthly_price") or 0),
                 utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "next_period"),
            )
        conn.commit()
        return conn.total_changes > 0
