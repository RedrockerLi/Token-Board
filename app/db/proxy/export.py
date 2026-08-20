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
        from app.db.proxy.billing import materialize_period_charges

        # Export is also a billing read boundary. Run the idempotent
        # materializer synchronously so a first sync cannot omit the current
        # recurring period while the 60-second background worker is asleep.
        materialize_period_charges(self.db_path)
        conn = self._connect()
        try:
            # The shadow may live outside data/ (e.g. data/tmp_dash/); use the
            # canonical schema root carried by this ProxyDatabase instance.
            dash_db = DashboardDatabase(
                target_path, schema_dir=self.schema_dir)

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
