"""ProxyDatabase methods for ProxyLifecycleMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


class ProxyLifecycleMixin:
    def delete_account(self, account_id: int, mode: str = "detach") -> dict:
        """Soft-delete an account. Returns {ok: bool, error: str}.

        The row is kept (id is permanent, never recycled) and flagged with
        deleted_at. The account stops being routed and disappears from lists
        (queries treat a past deleted_at as gone), but its historical
        request_log rows keep their account_id and the dashboard archive keeps
        showing the name (the accounts mirror preserves soft-deleted entries).
        request_log rows are NOT touched; they are cleaned 30 days after
        export by the normal high-water-mark cleanup.

        mode:
          "cascade" — also delete this account's local keys.
          "detach"  — unbind the keys (account_id → NULL, keys stay for reuse).
        aggregate_entries referencing this account are deleted so an aggregate
        chain never routes to a dead account.

        api accounts are always terminated immediately.  subscription accounts
        follow the configured default deletion operation:
          'immediate'     — deleted_at = now; local keys & aggregates cleaned up
                            right here.
          'end_of_period' — deleted_at = end of each key's current billing
                            period (the account keeps routing until then; local
                            keys & aggregates are kept so clients can still
                            reach it).  The cleanup intent is recorded in
                            deferred_cleanup_mode and performed by the deletion
                            finalizer once deleted_at has passed.
        """
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            real_id = (route["account_id"] if route and route["account_id"] is not None
                       else account_id)
            account = conn.execute(
                "SELECT a.created_at,a.valid_from,bc.charge_type,bc.cancellation_policy "
                "FROM accounts a LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
                "AND bc.valid_until IS NULL WHERE a.id=? AND a.lifecycle_state='active'",
                (real_id,),
            ).fetchone()
            if account is None:
                return {"ok": False, "error": "Account not found"}
            now = _utc_now()
            anchor = (_parse_iso_date(account["valid_from"])
                      or _parse_utc_timestamp(account["created_at"]).date())
            deferred = (account["charge_type"] == "recurring" and
                        account["cancellation_policy"] == "period_end")
            effective = (_period_start(
                _next_month(_billing_period_month(now, anchor.day)), anchor.day)
                         - timedelta(seconds=1)) if deferred else now
            timestamp = effective.strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("UPDATE accounts SET deleted_at=? WHERE id=?", (timestamp, real_id))
            upstream_ids = [row[0] for row in conn.execute(
                "SELECT id FROM upstreams WHERE account_id=?", (real_id,))]
            if upstream_ids:
                placeholders = ",".join("?" for _ in upstream_ids)
                conn.execute(
                    f"UPDATE upstream_credentials SET deleted_at=? WHERE upstream_id IN ({placeholders}) "
                    "AND deleted_at IS NULL", (timestamp, *upstream_ids))
            if not deferred:
                conn.execute("UPDATE accounts SET lifecycle_state='deleted' WHERE id=?", (real_id,))
                conn.execute("UPDATE upstreams SET enabled=0 WHERE account_id=?", (real_id,))
                conn.execute("UPDATE route_sets SET enabled=0 WHERE account_id=?", (real_id,))
                conn.execute(
                    "UPDATE client_keys SET enabled=0,deleted_at=? WHERE route_set_id IN "
                    "(SELECT id FROM route_sets WHERE account_id=?)",
                    (timestamp, real_id),
                )
                if upstream_ids:
                    placeholders = ",".join("?" for _ in upstream_ids)
                    conn.execute(
                        f"UPDATE route_rules SET enabled=0 WHERE upstream_id IN ({placeholders})",
                        upstream_ids)
            conn.commit()
            return {"ok": conn.total_changes > 0, "error": "",
                    "cancellation_mode": account["cancellation_policy"],
                    "cancelled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "effective_deleted_at": timestamp, "deferred": deferred}
        finally:
            conn.close()

    def finalize_deferred_deletions(self) -> int:
        """Complete end-of-period account deletions whose time has come.

        Routing already stopped at deleted_at (queries treat a past
        deleted_at as gone); this only finishes the cleanup that was deferred
        at delete time — detach/cascade the local keys and drop aggregate
        references — and clears the marker.  Idempotent, safe to call on every
        sweep.
        """
        conn = self._connect()
        try:
            now = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            pending = conn.execute(
                "SELECT id FROM accounts WHERE lifecycle_state='active' "
                "AND deleted_at IS NOT NULL AND deleted_at<=?", (now,)
            ).fetchall()
            for row in pending:
                real_id = row["id"]
                conn.execute("UPDATE accounts SET lifecycle_state='deleted' WHERE id=?",
                             (real_id,))
                conn.execute("UPDATE upstreams SET enabled=0 WHERE account_id=?", (real_id,))
                conn.execute("UPDATE route_sets SET enabled=0 WHERE account_id=?", (real_id,))
                conn.execute(
                    "UPDATE client_keys SET enabled=0,deleted_at=COALESCE(deleted_at,?) "
                    "WHERE route_set_id IN (SELECT id FROM route_sets WHERE account_id=?)",
                    (now, real_id),
                )
                conn.execute(
                    "UPDATE route_rules SET enabled=0 WHERE upstream_id IN "
                    "(SELECT id FROM upstreams WHERE account_id=?)", (real_id,))
            conn.commit()
            return len(pending)
        finally:
            conn.close()

    def update_account_models(self, account_id: int, models: list[str]) -> int:
        """Replace all models for an account. Returns count of models stored."""
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            if route is None or route["upstream_id"] is None:
                return 0
            conn.execute("DELETE FROM upstream_model_catalog WHERE upstream_id=?",
                         (route["upstream_id"],))
            conn.executemany(
                "INSERT OR IGNORE INTO upstream_model_catalog(upstream_id,model_id) VALUES(?,?)",
                [(route["upstream_id"], model) for model in models],
            )
            conn.commit()
            return len(models)
        finally:
            conn.close()

    def get_account_models(self, account_id: int) -> list[str]:
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            if route is None or route["upstream_id"] is None:
                return []
            rows = conn.execute(
                "SELECT model_id FROM upstream_model_catalog WHERE upstream_id=? ORDER BY model_id",
                (route["upstream_id"],),
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

    def get_plain_keys(self, account_id: int) -> list[str]:
        """Plaintext upstream keys of an account (server-side only — never
        sent to the client; used by the concurrency-test route to hit the
        upstream directly)."""
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            if route is None or route["upstream_id"] is None:
                return []
            rows = conn.execute(
                "SELECT s.secret_value FROM upstream_credentials c JOIN upstream_secrets s "
                "ON s.credential_uuid=c.uuid WHERE c.upstream_id=? "
                "AND c.disabled_at IS NULL "
                "AND (c.deleted_at IS NULL OR c.deleted_at>"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ORDER BY c.position,c.runtime_id", (route["upstream_id"],)
            ).fetchall()
            return [row[0] for row in rows if row[0]]
        finally:
            conn.close()

    def get_aggregates(self) -> list[dict]:
        """Aggregate accounts (is_aggregate=1) with their model entries."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id,name,created_at FROM route_sets WHERE account_id IS NULL "
                "AND enabled=1 ORDER BY id"
            ).fetchall()
            result = []
            for row in rows:
                entries = conn.execute(
                    "SELECT rr.id,rr.model_pattern pattern,u.account_id upstream_account_id,"
                    "a.name upstream_account_name,COALESCE(rr.target_model,rr.model_pattern) upstream_model "
                    "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
                    "LEFT JOIN accounts a ON a.id=u.account_id WHERE rr.route_set_id=? "
                    "AND rr.enabled=1 ORDER BY rr.priority,rr.id", (row["id"],)
                ).fetchall()
                result.append({**dict(row), "entries": [dict(entry) for entry in entries]})
            return result
        finally:
            conn.close()

    def create_aggregate(self, data: dict) -> int:
        """Create an aggregate account (is_aggregate=1) + its model entries."""
        conn = self._connect()
        try:
            aggregate_id = self._next_shared_id(conn)
            conn.execute(
                "INSERT INTO route_sets(id,uuid,name,account_id) VALUES(?,?,?,NULL)",
                (aggregate_id, str(uuid.uuid4()), data["name"]),
            )
            self._replace_v1_aggregate_rules(conn, aggregate_id, data.get("entries", []))
            conn.commit()
            return aggregate_id
        finally:
            conn.close()

    def update_aggregate(self, agg_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            if "name" in data:
                conn.execute(
                    "UPDATE route_sets SET name=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id=? AND account_id IS NULL",
                    (data["name"], agg_id),
                )
            if "entries" in data:
                self._replace_v1_aggregate_rules(conn, agg_id, data["entries"])
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_aggregate(self, agg_id: int) -> bool:
        """Soft-delete an aggregate account (id stays, row flagged deleted_at)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE route_sets SET enabled=0,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND account_id IS NULL AND enabled=1", (agg_id,))
            conn.execute("UPDATE route_rules SET enabled=0 WHERE route_set_id=?", (agg_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()
