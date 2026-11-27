"""ProxyDatabase methods for ProxyRoutingMixin."""

from app.db.proxy.common import _generate_key, sqlite3, uuid


class ProxyRoutingMixin:
    def _replace_v1_aggregate_rules(self, conn: sqlite3.Connection,
                                    route_set_id: int,
                                    entries: list[dict]) -> None:
        conn.execute("DELETE FROM route_rules WHERE route_set_id=?", (route_set_id,))
        for priority, entry in enumerate(entries):
            target = self._v1_route_account(conn, int(entry["account_id"]))
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
                "LEFT JOIN accounts a ON a.id=rs.account_id "
                "WHERE ck.enabled=1 "
                "AND (rs.account_id IS NULL OR "
                "(a.account_kind='proxy')) "
                "ORDER BY ck.id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _assert_routable_account(conn: sqlite3.Connection, account_id):
        """Reject binding a local key to a non-routable account.

        Legacy non-routable rows must never be used as a routable upstream,
        so no local key may point at one.
        """
        if account_id is None:
            return
        row = conn.execute(
            "SELECT 1 FROM route_sets rs LEFT JOIN accounts a "
            "ON a.id=rs.account_id WHERE rs.id=? AND rs.enabled=1 "
            "AND (rs.account_id IS NULL OR "
            "(a.account_kind='proxy'))",
            (account_id,),
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
        """Hard-delete a local key while preserving request history."""
        from app.db.proxy.deletion import purge_client_keys

        conn = self._connect()
        try:
            deleted = purge_client_keys(conn, [key_id])
            conn.commit()
            return deleted > 0
        finally:
            conn.close()

    @staticmethod
    def _insert_agent_usage_row(conn, software_id: int, model: str,
                                prompt_tokens: int, completion_tokens: int,
                                cache_read_tokens: int, total_tokens: int,
                                requested_at: str, event_id: str,
                                duration_ms: int = 0, output_tps: float = 0.0) -> bool:
        """Insert one imported software usage row on the caller's connection.

        A pending V1 event lets SQLite select the request-time price and FX.  A
        permanent receipt makes INSERT OR IGNORE idempotent even after the
        request_log retention window; `requested_at` must be a SQLite UTC timestamp
        "YYYY-MM-DD HH:MM:SS".
        Returns True when a row was inserted.
        """
        receipt = conn.execute(
            "INSERT OR IGNORE INTO agent_usage_receipts"
            "(event_id,software_id) VALUES(?,?)",
            (event_id, software_id),
        )
        if receipt.rowcount == 0:
            # 收据已存在:上次扫描时回复还没写完(无 stop_reason,没估耗时)或
            # 历史文件重新解析。只补写仍为空计时的行,不覆盖已有值。
            if duration_ms > 0:
                conn.execute("UPDATE request_log SET duration_ms=?,output_tps=? "
                             "WHERE event_id=? AND duration_ms=0",
                             (int(duration_ms), float(output_tps), event_id))
            # token 是计价数量:重扫若看到原次扫描看不到的 token,旧金额就是
            # 错的。收据拦住了整行写入,所以这里只把确有差异的那一行改回
            # 'pending' 并清除金额,由同一触发器权威(V2.12 的 AFTER UPDATE)
            # 按 requested_at 重新计价——不在 Python 里另写一份价格逻辑。
            # event_id 唯一,单行 UPDATE;金额已一致时不写。
            conn.execute(
                "UPDATE request_log SET prompt_tokens=?,completion_tokens=?,"
                "cache_read_tokens=?,total_tokens=?,pricing_status='pending',"
                "equivalent_cost=0,billed_usage_cost=0 "
                "WHERE event_id=? AND (prompt_tokens IS NOT ? "
                "OR completion_tokens IS NOT ? OR cache_read_tokens IS NOT ? "
                "OR total_tokens IS NOT ?)",
                (int(prompt_tokens), int(completion_tokens),
                 int(cache_read_tokens), int(total_tokens), event_id,
                 int(prompt_tokens), int(completion_tokens),
                 int(cache_read_tokens), int(total_tokens)),
            )
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO request_log"
            "(event_id,source_kind,account_id,agent_software_id,model,prompt_tokens,completion_tokens,"
            "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,"
            "is_streaming,status_code,duration_ms,output_tps,requested_at,pricing_status) "
            "VALUES(?,'import',?,?,?, ?,?,?,?,0,0,0,200,?,?,?,'pending')",
            (event_id, software_id, software_id, model, int(prompt_tokens), int(completion_tokens),
             int(cache_read_tokens), int(total_tokens), int(duration_ms), float(output_tps),
             requested_at),
        )
        return cur.rowcount > 0

    def insert_agent_usage(self, software_id: int, model: str,
                           prompt_tokens: int, completion_tokens: int,
                           cache_read_tokens: int, total_tokens: int,
                           requested_at: str, event_id: str,
                           duration_ms: int = 0, output_tps: float = 0.0) -> bool:
        """Insert one imported software usage row into request_log.

        Convenience wrapper opening its own connection (for manual/API use);
        the background importer calls :meth:`_insert_agent_usage_row` on a
        shared connection instead to avoid write-lock contention.
        """
        conn = self._connect()
        try:
            ok = self._insert_agent_usage_row(
                conn, software_id, model, prompt_tokens, completion_tokens,
                cache_read_tokens, total_tokens, requested_at, event_id,
                duration_ms=duration_ms, output_tps=output_tps,
            )
            conn.commit()
            return ok
        finally:
            conn.close()
