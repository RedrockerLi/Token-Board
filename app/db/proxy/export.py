"""ProxyDatabase methods for ProxyExportMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


class ProxyExportMixin:
    def export_to_dashboard(self, target_path: str, mark: int, max_id: int) -> dict:
        """Export request_log rows (id in (mark, max_id]) into a dashboard archive.

        Writes into *target_path* (the shadow archive built by sync_dashboard —
        cloud base + this machine's data) with additive upserts, exactly once per
        row. The high-water mark is advanced by the caller ONLY after the whole
        pull-export-upload transaction succeeds, so nothing here marks rows.

        plan subscription fees are persisted per (account, month), converted
        to CNY at the billing period's start-date rate and locked once
        determined; the current monthly_price still refreshes the current
        month on every export regardless of the batch window (Requirement 4:
        price edits affect only the current month).
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

            # Both proxy and agent usage now share the same daily grain.  The
            # account mirror carries the source kind, so the reader can label
            # an agent without a second dashboard table.
            # An active binding makes every materialized subscription period
            # an actual cost of each present software. The denominator is the
            # number of active bound agents, not the number of agents that
            # happened to produce usage.
            now = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            current_month = now[:7]
            subscription_rows = conn.execute(
                "SELECT s.id,s.uuid,c.period_start,c.recurring_charge,"
                "c.normalized_recurring_cost,c.currency,c.base_currency,c.fx_rate_date "
                "FROM agent_subscriptions s "
                "JOIN agent_subscription_instances i ON i.subscription_id=s.id "
                "JOIN agent_subscription_period_charges c ON c.instance_id=i.id "
                "WHERE (s.lifecycle_state='active' OR "
                "(s.lifecycle_state='deleted' AND s.valid_until>?)) "
                "AND (i.lifecycle_state='active' OR "
                "(i.lifecycle_state='deleted' AND i.valid_until>?)) "
                "ORDER BY s.id,c.period_start", (now, now)
            ).fetchall()
            totals: dict[int, dict[str, dict]] = {}
            subscription_units: dict[int, str] = {}
            for row in subscription_rows:
                month = str(row["period_start"])[:7]
                sid = int(row["id"])
                subscription_units[sid] = f"agent-subscription:{row['uuid']}"
                values = totals.setdefault(sid, {}).setdefault(month, {
                    "recurring_charge": 0.0,
                    "normalized_recurring_cost": 0.0,
                    "currency": row["currency"] or "CNY",
                    "base_currency": row["base_currency"] or "CNY",
                    "fx_rate_date": row["fx_rate_date"],
                    "billing_incomplete_count": 0,
                })
                values["recurring_charge"] += float(row["recurring_charge"] or 0)
                if row["normalized_recurring_cost"] is None:
                    values["billing_incomplete_count"] += 1
                    values["normalized_recurring_cost"] = None
                elif values["normalized_recurring_cost"] is not None:
                    values["normalized_recurring_cost"] += float(
                        row["normalized_recurring_cost"])
                values["fx_rate_date"] = row["fx_rate_date"] or values["fx_rate_date"]
            allocations: dict[tuple[int, str], dict[str, dict]] = {}
            for sid, periods in totals.items():
                binding_rows = conn.execute(
                    "SELECT b.software_id FROM agent_subscription_bindings b "
                    "JOIN agent_subscriptions parent ON parent.id=b.subscription_id "
                    "JOIN agent_software s ON s.id=b.software_id "
                    "JOIN accounts a ON a.id=s.id "
                    "WHERE b.subscription_id=? AND b.lifecycle_state='active' "
                    "AND a.account_kind='agent' AND a.lifecycle_state='active' "
                    "AND b.valid_from<=? "
                    "AND (b.valid_until IS NULL OR b.valid_until>?) "
                    "AND (parent.valid_until IS NULL OR parent.valid_until>?) "
                    "ORDER BY b.software_id", (sid, now, now, now)
                ).fetchall()
                denominator = len(binding_rows)
                if not denominator:
                    continue
                unit_id = subscription_units[sid]
                for binding in binding_rows:
                    allocations[(int(binding["software_id"]), unit_id)] = {
                        month: {
                            **values,
                            "recurring_charge": values["recurring_charge"] / denominator,
                            "normalized_recurring_cost": (
                                values["normalized_recurring_cost"] / denominator
                                if values["normalized_recurring_cost"] is not None else None),
                        }
                        for month, values in periods.items()
                    }
            dash_db.reconcile_agent_allocations(allocations, current_month)

            # B) Plan/agent subscriptions are derived from key lifecycles, not
            # from usage.  Reconcile every known lifecycle on every export so an
            # edited start date, cancellation, or scheduled price cannot leave
            # stale subscription rows behind.  Native prices are converted to
            # CNY at each period's start-date rate, locked once determined.
            metas = self._plan_key_billing_meta(conn)
            by_key_id = {meta["key_id"]: meta for meta in metas if meta["key_id"] is not None}
            by_account = {}
            active_units: dict[int, set[str]] = {}
            for meta in metas:
                by_account.setdefault(meta["account_id"], meta)
                active_units.setdefault(meta["account_id"], set()).add(
                    meta["billing_unit_id"])
                charges = conn.execute(
                    "SELECT period_start,normalized_recurring_cost "
                    "FROM billing_period_charges WHERE contract_id=? "
                    "AND credential_uuid IS ? "
                    "AND normalized_recurring_cost IS NOT NULL",
                    (meta["contract_id"], meta["credential_uuid"]),
                ).fetchall()
                normalized_periods = {
                    str(row["period_start"])[:7]:
                        float(row["normalized_recurring_cost"])
                    for row in charges
                }
                dash_db.reconcile_plan_subscription(
                    meta["account_id"], meta["billing_unit_id"],
                    normalized_periods,
                )
            for account_id, unit_ids in active_units.items():
                dash_db.cleanup_stale_subscription_units(account_id, unit_ids)

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
                requested = _parse_utc_timestamp(log["requested_at"])
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
                "dashboard_records": dash_count,
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
