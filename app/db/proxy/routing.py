"""ProxyDatabase methods for ProxyRoutingMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


class ProxyRoutingMixin:
    @staticmethod
    def _replace_v1_aggregate_rules(conn: sqlite3.Connection, route_set_id: int,
                                    entries: list[dict]) -> None:
        conn.execute("DELETE FROM route_rules WHERE route_set_id=?", (route_set_id,))
        for priority, entry in enumerate(entries):
            target = ProxyDatabase._v1_route_account(conn, int(entry["account_id"]))
            if target is None or target["upstream_id"] is None:
                raise ValueError(f"上游账户 {entry['account_id']} 不可路由")
            conn.execute(
                "INSERT INTO route_rules"
                "(route_set_id,model_pattern,priority,upstream_id,target_model) VALUES(?,?,?,?,?)",
                (route_set_id, entry["pattern"], priority, target["upstream_id"],
                 entry.get("upstream_model") or None),
            )

    def get_keys(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ck.id,ck.key_value,ck.label,ck.route_set_id account_id,"
                "rs.name account_name,COALESCE(u.api_format,'openai') account_format,"
                "ck.created_at,ck.last_used_at FROM client_keys ck "
                "JOIN route_sets rs ON rs.id=ck.route_set_id "
                "LEFT JOIN upstreams u ON u.account_id=rs.account_id AND u.enabled=1 "
                "WHERE ck.enabled=1 AND ck.deleted_at IS NULL ORDER BY ck.id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _assert_routable_account(conn: sqlite3.Connection, account_id):
        """Reject binding a local key to a non-routable account.

        Non-routable types (e.g. agent / Codex) are subscription-only: they
        must never be used as a routable upstream, so no local key may point
        at one.
        """
        if account_id is None:
            return
        row = conn.execute(
            "SELECT 1 FROM route_sets WHERE id=? AND enabled=1", (account_id,)
        ).fetchone()
        if row is None:
            raise ValueError("账户不可路由")

    def create_key(self, data: dict) -> str:
        """Create a new local key. Returns the generated key value."""
        key_value = _generate_key()
        conn = self._connect()
        try:
            self._assert_routable_account(conn, data.get("account_id"))
            conn.execute(
                "INSERT INTO client_keys(uuid,key_value,label,route_set_id) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), key_value, data.get("label", ""), data["account_id"]),
            )
            conn.commit()
            return key_value
        finally:
            conn.close()

    def update_key(self, key_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            if "account_id" in data:
                self._assert_routable_account(conn, data["account_id"])
            fields, values = [], []
            if "label" in data:
                fields.append("label=?")
                values.append(data["label"])
            if "account_id" in data:
                fields.append("route_set_id=?")
                values.append(data["account_id"])
            if not fields:
                return False
            conn.execute(f"UPDATE client_keys SET {','.join(fields)} WHERE id=?",
                         (*values, key_id))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_key(self, key_id: int) -> bool:
        """Hard-delete a local key. Its request_log rows are kept and their
        local_key_id is set to NULL via the ON DELETE SET NULL foreign key,
        so usage/billing data is preserved."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE client_keys SET enabled=0,deleted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND enabled=1", (key_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def get_agent_accounts(self) -> list[dict]:
        """Agent (subscription-only, import-driven) accounts, e.g. Codex.

        Never routable.  The import-driven type set comes from the spec so a
        future import-driven type flows through automatically.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT a.id,a.name,COALESCE(i.importer_kind,'') AS agent_kind,"
                "COALESCE(bc.currency,'CNY') AS currency,"
                "COALESCE((SELECT brecurring.recurring_price FROM billing_rate_events "
                "brecurring WHERE brecurring.contract_id=bc.id "
                "ORDER BY brecurring.effective_at DESC,brecurring.id DESC LIMIT 1),0) "
                "AS monthly_price FROM account_importers i "
                "JOIN accounts a ON a.id=i.account_id "
                "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
                "AND bc.valid_until IS NULL WHERE i.enabled=1 "
                "AND a.lifecycle_state='active' ORDER BY a.id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _insert_agent_usage_row(conn, account_id: int, model: str,
                                prompt_tokens: int, completion_tokens: int,
                                cache_read_tokens: int, total_tokens: int,
                                requested_at: str, event_id: str) -> bool:
        """Insert one agent (Codex) usage row on the caller's connection.

        A pending V1 event lets SQLite select the historical rate and FX.  The
        event_id UNIQUE constraint makes INSERT OR IGNORE idempotent across
        idempotent across crashes/restarts.  `requested_at` must be a SQLite UTC
        timestamp "YYYY-MM-DD HH:MM:SS".  Returns True when a row was inserted.
        """
        cur = conn.execute(
            "INSERT OR IGNORE INTO request_log"
            "(event_id,source_kind,account_id,model,prompt_tokens,completion_tokens,"
            "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,"
            "is_streaming,status_code,duration_ms,requested_at,pricing_status) "
            "VALUES(?,'import',?,?,?,?,?,?,0,0,0,200,0,?,'pending')",
            (event_id, account_id, model, int(prompt_tokens), int(completion_tokens),
             int(cache_read_tokens), int(total_tokens), requested_at),
        )
        return cur.rowcount > 0

    def insert_agent_usage(self, account_id: int, model: str,
                           prompt_tokens: int, completion_tokens: int,
                           cache_read_tokens: int, total_tokens: int,
                           requested_at: str, event_id: str) -> bool:
        """Insert one imported agent (Codex) usage row into request_log.

        Convenience wrapper opening its own connection (for manual/API use);
        the background importer calls :meth:`_insert_agent_usage_row` on a
        shared connection instead to avoid write-lock contention.
        """
        conn = self._connect()
        try:
            ok = self._insert_agent_usage_row(
                conn, account_id, model, prompt_tokens, completion_tokens,
                cache_read_tokens, total_tokens, requested_at, event_id)
            conn.commit()
            return ok
        finally:
            conn.close()
