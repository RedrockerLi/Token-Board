"""ProxyDatabase methods for ProxyExportMixin."""

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import _billing_period_month


class ProxyExportMixin:
    def export_to_dashboard(self, target_path: str, mark: int, max_id: int) -> dict:
        """Export request_log rows (id in (mark, max_id]) into a dashboard archive.

        Writes into *target_path* (the shadow archive built by sync_dashboard —
        cloud base + this machine's data) with additive upserts, exactly once per
        row. The high-water mark is advanced by the caller ONLY after the whole
        pull-export-upload transaction succeeds, so nothing here marks rows.

        plan subscription fees are persisted per (account, month) only after
        the source period charge is frozen. Request-derived virtual costs stay
        additive and are protected by the request-log high-water mark.
        """
        from app.db.dashboard_db import DashboardDatabase
        from app.db.proxy.billing import (
            materialize_all_period_charges,
        )

        # Export is also a billing read boundary. Run the idempotent
        # materializer synchronously so a first sync cannot omit the current
        # recurring period while the 60-second background worker is asleep.
        materialize_all_period_charges(self.db_path)
        conn = self._connect()
        try:
            # The shadow may live outside data/ (e.g. data/tmp_dash/); use the
            # canonical schema root carried by this ProxyDatabase instance.
            dash_db = DashboardDatabase(
                target_path, schema_dir=self.schema_dir)

            # The generic daily ledger has a foreign key to the mirror, so
            # identities must be upserted before usage rows.
            dash_db.upsert_account_batch([
                {"account_id": row["id"], "name": row["name"],
                 "lifecycle_state": row["lifecycle_state"],
                 "updated_at": row["updated_at"],
                 "account_kind": row["account_kind"]}
                for row in conn.execute(
                    "SELECT id,name,lifecycle_state,updated_at,account_kind "
                    "FROM accounts ORDER BY id")
            ])

            # A) usage + frozen cost: keyed by account_id (the identity). The
            #    display name comes from the dashboard `accounts` mirror.
            cost_columns = (
                "COALESCE(SUM(r.equivalent_cost),0) AS cost, "
                "COALESCE(SUM(r.billed_usage_cost),0) AS billed_usage_cost,"
            )
            rows = conn.execute(f"""
                SELECT
                    date(r.requested_at) AS date,
                    r.account_id,
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                    {cost_columns}
                    COUNT(*) AS request_count
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE r.id > ? AND r.id <= ?
                  AND a.id IS NOT NULL
                  AND a.account_kind IN ('proxy','agent')
                  AND LOWER(r.model) != 'unknown' AND r.model != ''
                  AND r.account_id IS NOT NULL
                  -- Only successful requests carry real usage; failed/aborted
                  -- requests (timeouts, auth/limit rejections, client
                  -- disconnect) record zero tokens and must not pollute the
                  -- usage archive (they stay in request_log for diagnostics).
                  AND r.status_code BETWEEN 200 AND 299
                GROUP BY date(r.requested_at), r.account_id, r.model
                ORDER BY date(r.requested_at), r.account_id, r.model
            """, (mark, max_id)).fetchall()

            dash_count = dash_db.upsert_proxy_batch([
                {
                    "date": r["date"], "model": r["model"],
                    "account_id": r["account_id"],
                    "prompt_tokens": r["prompt_tokens"],
                    "completion_tokens": r["completion_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "request_count": r["request_count"], "cost": r["cost"],
                    "billed_usage_cost": r["billed_usage_cost"],
                }
                for r in rows
            ])

            # B) Export only immutable, already-finalized subscription charges.
            # The source ledger is idempotent; the dashboard row becomes
            # immutable once its first frozen value is accepted.
            frozen_plan_rows = conn.execute(
                "SELECT c.period_start,c.recurring_charge,"
                "c.normalized_recurring_cost,c.currency,c.base_currency,"
                "c.fx_rate_date,c.finalized_at,c.credential_uuid,"
                "bc.uuid contract_uuid,bc.account_id,bc.billing_scope "
                "FROM billing_period_charges c "
                "JOIN billing_contracts bc ON bc.id=c.contract_id "
                "JOIN accounts a ON a.id=bc.account_id "
                "WHERE c.finalized_at IS NOT NULL AND a.account_kind='proxy' "
                "ORDER BY c.period_start,bc.account_id,c.credential_uuid"
            ).fetchall()
            frozen_charge_count = 0
            for charge in frozen_plan_rows:
                billing_unit_id = (
                    str(charge["credential_uuid"])
                    if charge["credential_uuid"] is not None
                    else f"contract:{charge['contract_uuid']}"
                )
                frozen_charge_count += dash_db.upsert_frozen_plan_charge(
                    month=str(charge["period_start"])[:7],
                    account_id=int(charge["account_id"]),
                    billing_unit_id=billing_unit_id,
                    recurring_charge=float(charge["recurring_charge"] or 0),
                    normalized_recurring_cost=charge["normalized_recurring_cost"],
                    currency=charge["currency"] or "CNY",
                    base_currency=charge["base_currency"] or "CNY",
                    fx_rate_date=charge["fx_rate_date"],
                    frozen_at=charge["finalized_at"],
                )

            frozen_agent_rows = conn.execute(
                "SELECT c.period_start,a.software_id,a.recurring_charge,"
                "a.normalized_recurring_cost,a.currency,a.base_currency,"
                "a.fx_rate_date,a.finalized_at,s.uuid subscription_uuid "
                "FROM agent_subscription_charge_allocations a "
                "JOIN agent_subscription_period_charges c "
                "ON c.id=a.period_charge_id "
                "JOIN agent_subscription_instances i ON i.id=c.instance_id "
                "JOIN agent_subscriptions s ON s.id=i.subscription_id "
                "WHERE c.finalized_at IS NOT NULL AND a.finalized_at IS NOT NULL "
                "ORDER BY c.period_start,a.software_id"
            ).fetchall()
            for allocation in frozen_agent_rows:
                frozen_charge_count += dash_db.upsert_frozen_agent_allocation(
                    month=str(allocation["period_start"])[:7],
                    account_id=int(allocation["software_id"]),
                    billing_unit_id=f"agent-subscription:{allocation['subscription_uuid']}",
                    recurring_charge=float(allocation["recurring_charge"] or 0),
                    normalized_recurring_cost=allocation["normalized_recurring_cost"],
                    currency=allocation["currency"] or "CNY",
                    base_currency=allocation["base_currency"] or "CNY",
                    fx_rate_date=allocation["fx_rate_date"],
                    frozen_at=allocation["finalized_at"],
                )

            # C) Request-log plan costs remain additive and are still protected
            # by the request high-water mark. They are separate from frozen
            # recurring charges.
            now = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            metas = self._plan_key_billing_meta(conn)
            by_key_id = {meta["key_id"]: meta for meta in metas if meta["key_id"] is not None}
            by_account = {}
            for meta in metas:
                by_account.setdefault(meta["account_id"], meta)

            virtual_buckets: dict[tuple[str, int, str], float] = {}
            plan_logs = conn.execute(
                "SELECT r.account_id,r.upstream_key_id,r.requested_at,"
                "r.equivalent_cost api_cost FROM request_log r "
                "JOIN billing_contracts bc ON bc.account_id=r.account_id "
                "JOIN accounts a ON a.id=r.account_id AND a.account_kind='proxy' "
                "WHERE r.id>? AND r.id<=? AND r.status_code BETWEEN 200 AND 299 "
                "AND bc.charge_type='recurring' AND bc.valid_from<=r.requested_at "
                "AND (bc.valid_until IS NULL OR bc.valid_until>r.requested_at)",
                (mark, max_id),
            ).fetchall()
            for log in plan_logs:
                meta = by_key_id.get(log["upstream_key_id"]) or by_account.get(log["account_id"])
                if meta is None or not meta.get("billing_unit_id"):
                    continue
                requested = parse_runtime_timestamp(log["requested_at"])
                month = _billing_period_month(requested, meta["anchor"].day)
                bucket = (month, meta["account_id"],
                          meta["billing_unit_id"])
                virtual_buckets[bucket] = virtual_buckets.get(bucket, 0.0) + float(log["api_cost"] or 0)
            for (month, account_id, billing_unit_id), virtual_cost in virtual_buckets.items():
                dash_db.accumulate_plan_summary(
                    month=month, account_id=account_id,
                    billing_unit_id=billing_unit_id,
                    subscription_cost=0.0, virtual_cost=virtual_cost,
                    refresh_subscription=False,
                )

            return {
                "record_count": len(rows),
                "dashboard_records": dash_count + frozen_charge_count,
            }
        finally:
            conn.close()

    def cleanup_exported_logs(self, mark: int, max_age_days: int = 30) -> int:
        """Delete archived request_log rows older than *max_age_days*.

        Only rows with id <= mark are deleted — those are the rows already
        counted into the archive (the high-water mark advances only after a
        successful upload). Rows with id > mark (never exported) are kept
        forever: a failed transaction never advances the mark, so nothing is
        ever lost.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM request_log "
                "WHERE id <= ? AND requested_at < datetime('now', '-' || ? || ' days')",
                (mark, str(max_age_days)),
            )
            conn.commit()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version // 10000 >= 1:
                conn.execute("PRAGMA incremental_vacuum(256)")
            return cursor.rowcount
        finally:
            conn.close()

    def get_export_mark(self) -> int:
        """Read the sync high-water mark (last successfully exported log id)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_exported_log_id'"
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            conn.close()

    def set_export_mark(self, max_id: int):
        """Advance the high-water mark (commit point of a successful sync)."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) "
                "VALUES ('last_exported_log_id', ?)",
                (str(max_id),),
            )
            conn.commit()
        finally:
            conn.close()

    def get_max_log_id(self) -> int:
        """Current max request_log id (upper bound for the next export batch)."""
        conn = self._connect()
        try:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM request_log").fetchone()[0]
        finally:
            conn.close()
