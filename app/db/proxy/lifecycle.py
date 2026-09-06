"""ProxyDatabase methods for ProxyLifecycleMixin."""

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import (
    _billing_period_month, _cancellation_end, _next_month,
    _parse_iso_date, _period_start, sqlite3, timedelta, uuid,
)


class ProxyLifecycleMixin:
    def delete_account(self, account_id: int, mode: str = "detach") -> dict:
        """Terminate a proxy account after its current charge is frozen.

        Recurring accounts are rejected until the period-start billing worker
        has written their immutable charge rows.  Deletion only consumes those
        rows: it never invokes the materializer.  Immediate deletion removes
        the live routing/credential/contract graph in this transaction while
        preserving ``account_identities``, request history and frozen ledgers.
        End-of-period deletion keeps the live graph until the configured
        boundary, when :meth:`finalize_deferred_deletions` performs the same
        purge.  ``mode`` remains an API compatibility parameter; hard deletion
        removes account-owned route sets and their keys in either mode.
        """
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            real_id = (route["account_id"] if route and route["account_id"] is not None
                       else account_id)
            account = conn.execute(
                "SELECT a.created_at,a.valid_from,bc.charge_type "
                "FROM accounts a LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
                "AND bc.valid_until IS NULL WHERE a.id=? AND a.account_kind='proxy' "
                "AND a.lifecycle_state='active'",
                (real_id,),
            ).fetchone()
            if account is None:
                return {"ok": False, "error": "Account not found"}
            now = utc_now()
            if account["charge_type"] == "recurring":
                from app.db.proxy.billing import proxy_billing_ready
                # Deletion is deliberately not a billing trigger.  The
                # period-start worker (or resource creation at today's
                # boundary) must have written the immutable charge first.
                if not proxy_billing_ready(conn, real_id, now):
                    return {"ok": False,
                            "error": "当前计费周期尚未固化，请等待账单周期任务完成后重试"}
            anchor = (_parse_iso_date(account["valid_from"])
                      or parse_runtime_timestamp(account["created_at"]).date())
            # The settings page and the delete confirmation both describe the
            # current global default. Read that same setting in this transaction
            # instead of relying on a client-side preview.
            config = self._billing_config_conn(conn)
            deferred = (account["charge_type"] == "recurring" and
                        config["cancellation_mode"] == "end_of_period")
            effective = (_period_start(
                _next_month(_billing_period_month(now, anchor.day)), anchor.day)
                         - timedelta(seconds=1)) if deferred else now
            timestamp = effective.strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute("UPDATE accounts SET deleted_at=? WHERE id=?", (timestamp, real_id))
            upstream_ids = [row[0] for row in conn.execute(
                "SELECT id FROM upstreams WHERE account_id=?", (real_id,))]
            if upstream_ids:
                placeholders = ",".join("?" for _ in upstream_ids)
                if deferred:
                    # A plan's keys are independent subscription units.  Do
                    # not anchor the whole upstream to the account/upstream
                    # creation date: calculate each key's current period from
                    # its own valid_from (or created_at), then keep the
                    # account alive until the latest key expires.
                    credential_rows = conn.execute(
                        f"SELECT uuid,valid_from,created_at,deleted_at "
                        f"FROM upstream_credentials WHERE upstream_id IN ({placeholders}) "
                        "AND (disabled_at IS NULL OR disabled_at>?) "
                        "AND (deleted_at IS NULL OR deleted_at>?)",
                        (*upstream_ids, now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         now.strftime("%Y-%m-%dT%H:%M:%SZ")),
                    ).fetchall()
                    key_expiries = []
                    for credential in credential_rows:
                        existing_end = parse_runtime_timestamp(credential["deleted_at"])
                        if existing_end is not None and existing_end > now:
                            end = existing_end
                        else:
                            key_anchor = (
                                _parse_iso_date(credential["valid_from"])
                                or parse_runtime_timestamp(credential["created_at"]).date()
                            )
                            end = _cancellation_end(
                                config, now, key_anchor.day, "plan")
                            conn.execute(
                                "UPDATE upstream_credentials SET deleted_at=? "
                                "WHERE uuid=? AND deleted_at IS NULL",
                                (end.strftime("%Y-%m-%dT%H:%M:%SZ"), credential["uuid"]),
                            )
                        key_expiries.append(end)
                    if key_expiries:
                        effective = max(key_expiries)
                        timestamp = effective.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                conn.execute(
                    f"UPDATE upstream_credentials SET deleted_at=? WHERE upstream_id IN ({placeholders}) "
                    "AND deleted_at IS NULL", (timestamp, *upstream_ids))
            # The account boundary is derived from the final per-key expiry
            # above, not necessarily from the account creation anchor.
            conn.execute("UPDATE accounts SET deleted_at=? WHERE id=?",
                         (timestamp, real_id))
            if not deferred:
                from app.db.proxy.deletion import purge_expired_secrets, purge_route_sets

                route_set_ids = [row[0] for row in conn.execute(
                    "SELECT id FROM route_sets WHERE account_id=?", (real_id,)
                ).fetchall()]
                purge_route_sets(conn, route_set_ids)
                conn.execute(
                    "DELETE FROM upstream_model_catalog WHERE upstream_id IN "
                    "(SELECT id FROM upstreams WHERE account_id=?)", (real_id,))
                purge_expired_secrets(conn, timestamp)
                conn.execute("UPDATE accounts SET lifecycle_state='deleted' WHERE id=?", (real_id,))
                conn.execute("UPDATE upstreams SET enabled=0 WHERE account_id=?", (real_id,))
                if upstream_ids:
                    placeholders = ",".join("?" for _ in upstream_ids)
                    conn.execute(
                        f"UPDATE route_rules SET enabled=0 WHERE upstream_id IN ({placeholders})",
                        upstream_ids)
                from app.db.proxy.deletion import purge_proxy_account
                purge_proxy_account(conn, real_id)
            conn.commit()
            return {"ok": conn.total_changes > 0, "error": "",
                    "cancellation_mode": ("end_of_period" if deferred
                                           else "immediate"),
                    "cancelled_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "effective_deleted_at": timestamp, "deferred": deferred}
        finally:
            conn.close()

    def cancel_account_deletion(self, account_id: int) -> dict:
        """Cancel an account deletion that has not reached its effective time.

        End-of-period cancellation deliberately leaves the account and its
        routing graph live, recording only future ``deleted_at`` timestamps.
        Reversing that operation therefore clears the account marker and the
        future credential markers in one transaction.  An account whose
        deadline has passed is not recoverable here: the finalizer may already
        have disabled its routing graph, and treating that state as pending
        would make the result depend on a race with the background sweep.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now()
            now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            route = self._v1_route_account(conn, account_id)
            real_id = (route["account_id"] if route and route["account_id"] is not None
                       else account_id)
            account = conn.execute(
                "SELECT id,deleted_at FROM accounts "
                "WHERE id=? AND account_kind='proxy' AND lifecycle_state='active' "
                "AND deleted_at IS NOT NULL AND deleted_at>?",
                (real_id, now_text),
            ).fetchone()
            if account is None:
                conn.rollback()
                return {
                    "ok": False,
                    "error": "Account deletion is not pending or has expired",
                }

            restored = conn.execute(
                "UPDATE upstream_credentials SET deleted_at=NULL "
                "WHERE upstream_id IN (SELECT id FROM upstreams WHERE account_id=?) "
                "AND deleted_at IS NOT NULL AND deleted_at>?",
                (real_id, now_text),
            )
            conn.execute(
                "UPDATE accounts SET deleted_at=NULL,updated_at=? WHERE id=?",
                (now_text, real_id),
            )
            conn.commit()
            return {
                "ok": True,
                "error": "",
                "cancelled_at": now_text,
                "restored_credentials": restored.rowcount,
            }
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_deferred_deletions(self) -> int:
        """Physically purge deferred accounts after their effective boundary.

        Idempotent and safe to call on every maintenance sweep.  Historical
        identities, request rows and frozen financial facts remain available
        for reporting/export.
        """
        conn = self._connect()
        try:
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            from app.db.proxy.deletion import purge_expired_secrets

            purge_expired_secrets(conn, now)
            pending = conn.execute(
                "SELECT id FROM accounts WHERE lifecycle_state='active' "
                "AND account_kind='proxy' AND deleted_at IS NOT NULL AND deleted_at<=?",
                (now,)
            ).fetchall()
            for row in pending:
                real_id = row["id"]
                conn.execute("UPDATE accounts SET lifecycle_state='deleted' WHERE id=?",
                             (real_id,))
                from app.db.proxy.deletion import purge_route_sets

                route_set_ids = [item[0] for item in conn.execute(
                    "SELECT id FROM route_sets WHERE account_id=?", (real_id,)
                ).fetchall()]
                purge_route_sets(conn, route_set_ids)
                conn.execute(
                    "DELETE FROM upstream_model_catalog WHERE upstream_id IN "
                    "(SELECT id FROM upstreams WHERE account_id=?)", (real_id,))
                conn.execute("UPDATE upstreams SET enabled=0 WHERE account_id=?", (real_id,))
                conn.execute(
                    "UPDATE route_rules SET enabled=0 WHERE upstream_id IN "
                    "(SELECT id FROM upstreams WHERE account_id=?)", (real_id,))
                from app.db.proxy.deletion import purge_proxy_account
                purge_proxy_account(conn, real_id)
            # Agent bindings and rate events are live configuration.  Their
            # frozen charges/allocations do not reference the binding rows, so
            # expired rows can be removed safely; parent subscription and
            # instance rows remain when financial FKs still point at them.
            conn.execute(
                "DELETE FROM agent_subscription_bindings "
                "WHERE valid_until IS NOT NULL AND valid_until<=?", (now,)
            )
            conn.execute(
                "DELETE FROM agent_subscription_rate_events "
                "WHERE instance_id IN (SELECT id FROM agent_subscription_instances "
                "WHERE valid_until IS NOT NULL AND valid_until<=?)", (now,)
            )
            # A subscription with no issued financial fact is pure live
            # configuration and can be removed completely once expired.  Any
            # parent/instance referenced by a charge remains as an immutable
            # historical shell.
            conn.execute(
                "DELETE FROM agent_subscription_instances WHERE "
                "valid_until IS NOT NULL AND valid_until<=? AND NOT EXISTS ("
                "SELECT 1 FROM agent_subscription_period_charges c "
                "WHERE c.instance_id=agent_subscription_instances.id)", (now,)
            )
            conn.execute(
                "DELETE FROM agent_subscriptions WHERE "
                "valid_until IS NOT NULL AND valid_until<=? AND NOT EXISTS ("
                "SELECT 1 FROM agent_subscription_period_charges c "
                "WHERE c.subscription_id=agent_subscriptions.id) AND NOT EXISTS ("
                "SELECT 1 FROM agent_subscription_instances i "
                "WHERE i.subscription_id=agent_subscriptions.id)", (now,)
            )
            # Software parser configuration has no financial FK of its own;
            # request_log.agent_software_id is intentionally a historical
            # scalar.  Keep the shared account identity, but remove the live
            # parser/runtime rows after the software is deleted.
            conn.execute(
                "DELETE FROM agent_software_runtime WHERE software_id IN ("
                "SELECT id FROM accounts WHERE account_kind='agent' "
                "AND lifecycle_state='deleted')")
            conn.execute(
                "DELETE FROM agent_software WHERE id IN ("
                "SELECT id FROM accounts WHERE account_kind='agent' "
                "AND lifecycle_state='deleted')")
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
                    "JOIN accounts a ON a.id=u.account_id AND a.account_kind='proxy' "
                    "WHERE rr.route_set_id=? AND rr.enabled=1 "
                    "ORDER BY rr.priority,rr.id", (row["id"],)
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
        """Physically delete an aggregate and its attached local keys."""
        from app.db.proxy.deletion import purge_route_sets

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM route_sets WHERE id=? AND account_id IS NULL "
                "AND enabled=1", (agg_id,)).fetchone()
            if row is None:
                return False
            deleted = purge_route_sets(conn, [agg_id])
            conn.commit()
            return deleted > 0
        finally:
            conn.close()
