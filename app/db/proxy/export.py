"""ProxyDatabase methods for ProxyExportMixin."""

import hashlib
import json

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import _billing_period_month


def _billing_event_payload_hash(event) -> str:
    payload = {
        key: event[key] for key in (
            "event_key", "event_kind", "account_id", "account_uuid",
            "account_kind",
            "month", "billing_unit_id", "recurring_charge",
            "normalized_recurring_cost", "currency", "base_currency",
            "fx_rate_date",
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProxyExportMixin:
    def export_to_dashboard(self, target_path: str, mark: int, max_id: int,
                            billing_mark: int | None = None,
                            billing_max_id: int | None = None) -> dict:
        """Export request_log rows (id in (mark, max_id]) into a dashboard archive.

        Writes into *target_path* (the shadow archive built by sync_dashboard —
        cloud base + this machine's data) with additive upserts, exactly once per
        row. The high-water mark is advanced by the caller ONLY after the whole
        pull-export-upload transaction succeeds, so nothing here marks rows.

        plan subscription fees are persisted per (account, month) only after
        the source period charge is frozen. Billing events have their own
        high-water mark and are acknowledged by the caller only after the
        dashboard candidate is committed.
        """
        from app.db.dashboard_db import DashboardDatabase
        from app.db.proxy.billing import (
            materialize_all_period_charges,
        )

        # Export is also a billing read boundary. Run the idempotent
        # materializer synchronously so a first sync cannot omit the current
        # recurring period while the 60-second background worker is asleep.
        materialize_all_period_charges(self.db_path)
        if billing_mark is None:
            billing_mark = self.get_billing_export_mark()
        if billing_max_id is None:
            billing_max_id = self.get_max_billing_event_id()
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
            # Historical identities outlive the live accounts row.  Mirror
            # them as deleted identities so a later export can still render
            # the name after real-time configuration has been purged.
            dash_db.upsert_account_batch([
                {"account_id": row["id"], "name": row["name"],
                 "lifecycle_state": "active" if row["live_id"] is not None else "deleted",
                 "updated_at": row["updated_at"],
                 "account_kind": row["account_kind"]}
                for row in conn.execute(
                    "SELECT i.id,i.name,i.account_kind,i.updated_at,a.id AS live_id "
                    "FROM account_identities i LEFT JOIN accounts a ON a.id=i.id "
                    "ORDER BY i.id")
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
                    COALESCE(r.account_identity_id,r.account_id) AS account_id,
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                    {cost_columns}
                    COUNT(*) AS request_count
                FROM request_log r
                LEFT JOIN account_identities ai
                  ON ai.id=COALESCE(r.account_identity_id,r.account_id)
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE r.id > ? AND r.id <= ?
                  AND COALESCE(ai.account_kind,a.account_kind) IN ('proxy','agent')
                  AND LOWER(r.model) != 'unknown' AND r.model != ''
                  AND r.account_id IS NOT NULL
                  -- Only successful requests carry real usage; failed/aborted
                  -- requests (timeouts, auth/limit rejections, client
                  -- disconnect) record zero tokens and must not pollute the
                  -- usage archive (they stay in request_log for diagnostics).
                  AND r.status_code BETWEEN 200 AND 299
                GROUP BY date(r.requested_at), COALESCE(r.account_identity_id,r.account_id), r.model
                ORDER BY date(r.requested_at), COALESCE(r.account_identity_id,r.account_id), r.model
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

            # B) Export each immutable billing event at most once per shared
            # dashboard archive. The source event stream is separate from the
            # request-log stream and its mark is acknowledged by the caller.
            events = conn.execute(
                "SELECT * FROM billing_export_events "
                "WHERE id>? AND id<=? ORDER BY id",
                (billing_mark, billing_max_id),
            ).fetchall()
            frozen_charge_count = 0
            for event in events:
                event_dict = dict(event)
                frozen_charge_count += dash_db.record_billing_export_event(
                    event_dict, _billing_event_payload_hash(event_dict))

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
                "SELECT COALESCE(r.account_identity_id,r.account_id) account_id,"
                "r.billing_unit_id,r.billing_anchor_day,r.upstream_key_id,r.requested_at,"
                "r.equivalent_cost api_cost FROM request_log r "
                "LEFT JOIN billing_contracts bc ON bc.account_id=r.account_id "
                "LEFT JOIN account_identities ai ON ai.id=COALESCE(r.account_identity_id,r.account_id) "
                "WHERE r.id>? AND r.id<=? AND r.status_code BETWEEN 200 AND 299 "
                "AND ai.account_kind='proxy' AND "
                "(r.billing_unit_id IS NOT NULL OR (bc.charge_type='recurring' "
                "AND bc.valid_from<=r.requested_at AND "
                "(bc.valid_until IS NULL OR bc.valid_until>r.requested_at)))",
                (mark, max_id),
            ).fetchall()
            for log in plan_logs:
                meta = by_key_id.get(log["upstream_key_id"]) or by_account.get(log["account_id"])
                if log["billing_unit_id"]:
                    meta = dict(meta or {})
                    meta["billing_unit_id"] = log["billing_unit_id"]
                    meta["account_id"] = log["account_id"]
                if meta is None or not meta.get("billing_unit_id"):
                    continue
                requested = parse_runtime_timestamp(log["requested_at"])
                anchor_day = (log["billing_anchor_day"] or
                              (meta.get("anchor").day if meta.get("anchor") else 1))
                month = _billing_period_month(requested, int(anchor_day))
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
                "billing_event_count": len(events),
                "billing_max_id": billing_max_id,
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

    def get_billing_export_mark(self) -> int:
        """Read the independent immutable-billing export high-water mark."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM sync_state "
                "WHERE key='last_exported_billing_event_id'"
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            conn.close()

    def get_max_billing_event_id(self) -> int:
        """Return the export upper bound after materialization."""
        conn = self._connect()
        try:
            return int(conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM billing_export_events"
            ).fetchone()[0])
        finally:
            conn.close()

    def set_billing_export_mark(self, max_id: int) -> None:
        """Advance the billing export mark after dashboard commit."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state(key,value) VALUES(?,?)",
                ("last_exported_billing_event_id", str(max_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def set_export_marks(self, max_log_id: int,
                         max_billing_event_id: int | None = None) -> None:
        """Atomically acknowledge request and billing export streams."""
        conn = self._connect()
        try:
            values = [("last_exported_log_id", str(max_log_id))]
            if max_billing_event_id is not None:
                values.append(("last_exported_billing_event_id",
                               str(max_billing_event_id)))
            conn.executemany(
                "INSERT OR REPLACE INTO sync_state(key,value) VALUES(?,?)",
                values,
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
