"""The single live-resource lifecycle module for Token-Board V2."""

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import _cancellation_end, _parse_iso_date, sqlite3, uuid


class ProxyLifecycleMixin:
    def delete_account(self, account_id: int, mode: str = "detach") -> dict:
        """Delete or schedule one proxy graph, without a tombstone."""
        del mode
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            real_id = (route["account_id"] if route and route["account_id"] is not None
                       else account_id)
            account = conn.execute(
                "SELECT a.created_at,a.valid_from,bc.id contract_id,bc.charge_type,"
                "bc.ends_at FROM accounts a LEFT JOIN billing_contracts bc "
                "ON bc.account_id=a.id "
                "WHERE a.id=? AND a.account_kind='proxy' "
                "ORDER BY CASE WHEN bc.ends_at IS NULL THEN 0 ELSE 1 END,bc.id DESC "
                "LIMIT 1", (real_id,),
            ).fetchone()
            if account is None:
                return {"ok": False, "error": "Account not found"}
            now = utc_now()
            now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if (account["ends_at"] is not None and
                    account["ends_at"] > now_text):
                conn.rollback()
                return {"ok": False, "status": 409,
                        "error": "账户已进入结束流程，请先撤销结束"}
            if account["charge_type"] == "recurring":
                from app.db.proxy.billing import (
                    materialize_period_charges_conn, proxy_billing_ready,
                )
                materialize_period_charges_conn(conn, now, current_only=True)
                pending = (not proxy_billing_ready(conn, real_id, now) or
                           conn.execute(
                               "SELECT 1 FROM billing_period_charges c "
                               "JOIN billing_contracts bc ON bc.id=c.contract_id "
                               "WHERE bc.account_id=? AND c.period_start=("
                               "SELECT max(c2.period_start) FROM billing_period_charges c2 "
                               "WHERE c2.contract_id=c.contract_id AND c2.period_start<=?) "
                               "AND c.finalized_at IS NULL LIMIT 1",
                               (real_id, now_text),
                           ).fetchone() is not None)
                if pending:
                    conn.rollback()
                    return {"ok": False, "status": 409,
                            "error": "当前计费周期汇率未能固化，删除已取消"}

            config = self._billing_config_conn(conn)
            deferred = (account["charge_type"] == "recurring" and
                        config["cancellation_mode"] == "end_of_period")
            if not deferred:
                from app.db.proxy.deletion import purge_proxy_account
                purge_proxy_account(conn, real_id)
                effective = None
            else:
                rows = conn.execute(
                    "SELECT c.uuid,c.valid_from,c.created_at,c.ends_at "
                    "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id "
                    "WHERE u.account_id=? AND (c.ends_at IS NULL OR c.ends_at>?)",
                    (real_id, now_text),
                ).fetchall()
                expiries = []
                for row in rows:
                    end = parse_runtime_timestamp(row["ends_at"])
                    if end is None or end <= now:
                        anchor = (_parse_iso_date(row["valid_from"]) or
                                  parse_runtime_timestamp(row["created_at"]).date())
                        end = _cancellation_end(config, now, anchor.day, "plan")
                        conn.execute("UPDATE upstream_credentials SET ends_at=? WHERE uuid=?",
                                     (end.strftime("%Y-%m-%dT%H:%M:%SZ"), row["uuid"]))
                    expiries.append(end)
                effective_dt = max(expiries, default=now)
                effective = effective_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute("UPDATE billing_contracts SET ends_at=? WHERE id=?",
                             (effective, account["contract_id"]))
            conn.commit()
            return {
                "ok": True,
                "error": "",
                "cancellation_mode": "end_of_period" if deferred else "immediate",
                "cancelled_at": now_text,
                "effective_ends_at": effective,
                "deferred": deferred,
            }
        finally:
            conn.close()

    def cancel_account_deletion(self, account_id: int) -> dict:
        """Clear only future ends_at values; deleted rows are never restored."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now_text = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            route = self._v1_route_account(conn, account_id)
            real_id = (route["account_id"] if route and route["account_id"] is not None
                       else account_id)
            exists = conn.execute(
                "SELECT 1 FROM accounts a WHERE a.id=? AND a.account_kind='proxy' "
                "AND EXISTS (SELECT 1 FROM billing_contracts bc "
                "WHERE bc.account_id=a.id AND bc.ends_at>?)",
                (real_id, now_text),
            ).fetchone()
            if exists is None:
                conn.rollback()
                return {"ok": False,
                        "error": "Account deletion is not pending or has expired"}
            restored = conn.execute(
                "UPDATE upstream_credentials SET ends_at=NULL "
                "WHERE upstream_id IN (SELECT id FROM upstreams WHERE account_id=?) "
                "AND ends_at>?", (real_id, now_text),
            )
            conn.execute("UPDATE billing_contracts SET ends_at=NULL WHERE account_id=?",
                         (real_id,))
            conn.execute("UPDATE accounts SET updated_at=? WHERE id=?",
                         (now_text, real_id))
            conn.commit()
            return {"ok": True, "error": "", "cancelled_at": now_text,
                    "restored_credentials": restored.rowcount}
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_deferred_deletions(self) -> int:
        """Physically delete ended Plan units and empty parent graphs."""
        conn = self._connect()
        try:
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            from app.db.proxy.deletion import purge_credential, purge_proxy_account
            expired = conn.execute(
                "SELECT c.uuid FROM upstream_credentials c "
                "WHERE c.ends_at IS NOT NULL AND c.ends_at<=?", (now,),
            ).fetchall()
            for row in expired:
                purge_credential(conn, row["uuid"])
            pending = conn.execute(
                "SELECT a.id FROM accounts a JOIN billing_contracts bc "
                "ON bc.account_id=a.id WHERE a.account_kind='proxy' "
                "AND bc.ends_at IS NOT NULL AND bc.ends_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=a.id)",
                (now,),
            ).fetchall()
            for row in pending:
                purge_proxy_account(conn, int(row["id"]))

            conn.execute("DELETE FROM agent_subscription_bindings "
                         "WHERE ends_at IS NOT NULL AND ends_at<=?", (now,))
            from app.db.proxy.deletion import (
                purge_agent_subscription, purge_agent_subscription_instance,
            )
            instances = conn.execute(
                "SELECT id FROM agent_subscription_instances "
                "WHERE ends_at IS NOT NULL AND ends_at<=?", (now,),
            ).fetchall()
            for row in instances:
                purge_agent_subscription_instance(conn, int(row["id"]))
            subscriptions = conn.execute(
                "SELECT s.id FROM agent_subscriptions s "
                "WHERE s.ends_at IS NOT NULL AND s.ends_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM agent_subscription_instances i "
                "WHERE i.subscription_id=s.id)", (now,),
            ).fetchall()
            for row in subscriptions:
                purge_agent_subscription(conn, int(row["id"]))
            conn.commit()
            return len(expired) + len(instances) + len(subscriptions)
        finally:
            conn.close()

    def update_account_models(self, account_id: int, models: list[str]) -> int:
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
            return [row[0] for row in conn.execute(
                "SELECT model_id FROM upstream_model_catalog "
                "WHERE upstream_id=? ORDER BY model_id", (route["upstream_id"],)
            ).fetchall()]
        finally:
            conn.close()

    def get_plain_keys(self, account_id: int) -> list[str]:
        conn = self._connect()
        try:
            route = self._v1_route_account(conn, account_id)
            if route is None or route["upstream_id"] is None:
                return []
            rows = conn.execute(
                "SELECT s.secret_value FROM upstream_credentials c "
                "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                "WHERE c.upstream_id=? AND c.enabled=1 "
                "AND (c.ends_at IS NULL OR c.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
                "ORDER BY c.position,c.runtime_id", (route["upstream_id"],),
            ).fetchall()
            return [row[0] for row in rows if row[0]]
        finally:
            conn.close()

    def get_aggregates(self) -> list[dict]:
        conn = self._connect()
        try:
            result = []
            for row in conn.execute(
                "SELECT id,name,created_at FROM route_sets "
                "WHERE account_id IS NULL AND enabled=1 ORDER BY id"
            ):
                entries = conn.execute(
                    "SELECT rr.id,rr.model_pattern pattern,u.account_id upstream_account_id,"
                    "a.name upstream_account_name,COALESCE(rr.target_model,rr.model_pattern) upstream_model "
                    "FROM route_rules rr JOIN upstreams u ON u.id=rr.upstream_id "
                    "JOIN accounts a ON a.id=u.account_id AND a.account_kind='proxy' "
                    "WHERE rr.route_set_id=? AND rr.enabled=1 ORDER BY rr.priority,rr.id",
                    (row["id"],),
                ).fetchall()
                result.append({**dict(row), "entries": [dict(item) for item in entries]})
            return result
        finally:
            conn.close()

    def create_aggregate(self, data: dict) -> int:
        conn = self._connect()
        try:
            aggregate_id = self._next_shared_id(conn)
            conn.execute("INSERT INTO route_sets(id,uuid,name,account_id) VALUES(?,?,?,NULL)",
                         (aggregate_id, str(uuid.uuid4()), data["name"]))
            self._replace_v1_aggregate_rules(conn, aggregate_id, data.get("entries", []))
            conn.commit()
            return aggregate_id
        finally:
            conn.close()

    def update_aggregate(self, agg_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            if "name" in data:
                conn.execute("UPDATE route_sets SET name=?,updated_at=? "
                             "WHERE id=? AND account_id IS NULL",
                             (data["name"], utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), agg_id))
            if "entries" in data:
                self._replace_v1_aggregate_rules(conn, agg_id, data["entries"])
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_aggregate(self, agg_id: int) -> bool:
        from app.db.proxy.deletion import purge_route_sets
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id FROM route_sets WHERE id=? AND account_id IS NULL AND enabled=1",
                (agg_id,),
            ).fetchone()
            if row is None:
                return False
            deleted = purge_route_sets(conn, [agg_id])
            conn.commit()
            return deleted > 0
        finally:
            conn.close()
