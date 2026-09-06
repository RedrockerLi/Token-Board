"""DashboardWriterMixin implementation."""

import sqlite3
from datetime import datetime, timezone

from app.core import sqlite_runtime
from app.db.dashboard.common import (
    MODEL_ORDER, _parse_date, _sort_models, _track_recency,
)


class DashboardWriterMixin:
    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        # schema_dir may be passed explicitly when db_path is a shadow/temp
        # copy outside the standard data/ layout.  This guard is read-only;
        # Python startup is the only place allowed to mutate schema state.
        from app.db.migrations import schema_dir_for
        from app.db.schema_upgrade import verify_current_database
        self.schema_dir = schema_dir or schema_dir_for(self.db_path, "dashboard")
        verify_current_database(self.db_path, "dashboard", self.schema_dir)

    def _connect(self) -> sqlite3.Connection:
        return sqlite_runtime.connect(self.db_path, "dashboard_runtime")

    def upsert_account_batch(self, rows: list[dict]) -> int:
        """Mirror proxy identities, including whether they are agents."""
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO accounts
                   (account_id,name,lifecycle_state,updated_at,account_kind)
                   VALUES(:account_id,:name,:lifecycle_state,:updated_at,:account_kind)
                   ON CONFLICT(account_id) DO UPDATE SET
                     name=excluded.name,
                     lifecycle_state=excluded.lifecycle_state,
                     updated_at=excluded.updated_at,
                     account_kind=excluded.account_kind""",
                rows,
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def purge_accounts(self, account_ids: set[int] | list[int] | tuple[int, ...]) -> int:
        """Remove all dashboard archive rows for the given identities."""
        ids = sorted({int(account_id) for account_id in (account_ids or [])})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        conn = self._connect()
        try:
            deleted = 0
            conn.execute(
                """INSERT OR IGNORE INTO billing_export_receipts
                   (event_key,account_id,month,billing_unit_id,payload_hash)
                   SELECT 'legacy:' || account_id || ':' || month || ':' || billing_unit_id,
                          account_id,month,billing_unit_id,''
                   FROM monthly_recurring_costs
                   WHERE account_id IN ({}) AND charge_frozen_at IS NOT NULL""".format(
                       placeholders),
                ids,
            )
            for table in ("monthly_recurring_costs", "daily_usage"):
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE account_id IN ({placeholders})",
                    ids,
                ).rowcount
            deleted += conn.execute(
                f"DELETE FROM accounts WHERE account_id IN ({placeholders})",
                ids,
            ).rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    def upsert_proxy_data(self, date: str, model: str,
                          account_id: int, prompt_tokens: int,
                          completion_tokens: int, cache_read_tokens: int,
                          request_count: int,
                          cost: float = 0.0,
                          billed_usage_cost: float | None = None) -> int:
        """Insert proxy usage into the normalized ``daily_usage`` grain.

        ``cost`` is the historical equivalent cost and
        ``billed_usage_cost`` is the actual metered cost. Both values are
        copied from the proxy ledger and never recomputed by the dashboard.
        Buckets are keyed by stable ``account_id`` and additive export upserts
        preserve the request-log high-water-mark semantics.
        """
        conn = self._connect()
        try:
            billed = cost if billed_usage_cost is None else billed_usage_cost
            conn.execute(
                """INSERT INTO daily_usage
                   (date,account_id,model,input_tokens,cache_tokens,output_tokens,
                    request_count,equivalent_cost,billed_usage_cost)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(date,account_id,model) DO UPDATE SET
                     input_tokens=daily_usage.input_tokens+excluded.input_tokens,
                     cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
                     output_tokens=daily_usage.output_tokens+excluded.output_tokens,
                     request_count=daily_usage.request_count+excluded.request_count,
                     equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
                     billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost""",
                (date, account_id, model, prompt_tokens, cache_read_tokens,
                 completion_tokens, request_count, cost, billed),
            )
            conn.commit()
            return 1
        finally:
            conn.close()

    def upsert_proxy_batch(self, rows: list[dict]) -> int:
        """Bulk export using one connection and one transaction."""
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO daily_usage
                   (date,account_id,model,input_tokens,cache_tokens,output_tokens,
                    request_count,equivalent_cost,billed_usage_cost)
                   VALUES(:date,:account_id,:model,:prompt_tokens,:cache_read_tokens,
                          :completion_tokens,:request_count,:cost,:billed_usage_cost)
                   ON CONFLICT(date,account_id,model) DO UPDATE SET
                     input_tokens=daily_usage.input_tokens+excluded.input_tokens,
                     cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
                     output_tokens=daily_usage.output_tokens+excluded.output_tokens,
                     request_count=daily_usage.request_count+excluded.request_count,
                     equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
                     billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost""",
                rows,
            )
            conn.commit()
            return len(rows)
        finally:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                # An earlier SQLite failure may already have closed it.
                conn = None

    def upsert_frozen_plan_charge(self, *, month: str, account_id: int,
                                  billing_unit_id: str, recurring_charge: float,
                                  normalized_recurring_cost: float | None,
                                  currency: str, base_currency: str,
                                  fx_rate_date: str | None,
                                  frozen_at: str) -> int:
        """Insert one immutable recurring charge, updating only an unfrozen row."""
        if (recurring_charge == 0 and (normalized_recurring_cost or 0) == 0):
            return 0
        conn = self._connect()
        try:
            cursor = conn.execute(
                """INSERT INTO monthly_recurring_costs
                   (month,account_id,billing_unit_id,recurring_charge,equivalent_cost,
                    currency,normalized_recurring_cost,base_currency,fx_rate_date,
                    charge_frozen_at)
                   VALUES(?,?,?,?,0,?,?,?,?,?)
                   ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
                     recurring_charge=excluded.recurring_charge,
                     currency=excluded.currency,
                     normalized_recurring_cost=excluded.normalized_recurring_cost,
                     base_currency=excluded.base_currency,
                     fx_rate_date=excluded.fx_rate_date,
                     charge_frozen_at=excluded.charge_frozen_at
                   WHERE monthly_recurring_costs.charge_frozen_at IS NULL""",
                (month, account_id, billing_unit_id, recurring_charge, currency,
                 normalized_recurring_cost, base_currency, fx_rate_date, frozen_at),
            )
            conn.commit()
            return max(cursor.rowcount, 0)
        finally:
            conn.close()

    def upsert_frozen_agent_allocation(self, *, month: str, account_id: int,
                                       billing_unit_id: str,
                                       recurring_charge: float,
                                       normalized_recurring_cost: float | None,
                                       currency: str, base_currency: str,
                                       fx_rate_date: str | None,
                                       frozen_at: str) -> int:
        return self.upsert_frozen_plan_charge(
            month=month, account_id=account_id,
            billing_unit_id=billing_unit_id,
            recurring_charge=recurring_charge,
            normalized_recurring_cost=normalized_recurring_cost,
            currency=currency, base_currency=base_currency,
            fx_rate_date=fx_rate_date, frozen_at=frozen_at)

    def upsert_agent_software(self, rows: list[dict]) -> int:
        """Compatibility adapter: agent names live in the generic mirror."""
        return self.upsert_account_batch([
            {"account_id": row["software_id"], "name": row["name"],
             "lifecycle_state": "active",
             "updated_at": row.get("updated_at"), "account_kind": "agent"}
            for row in rows
        ])

    def upsert_agent_batch(self, rows: list[dict]) -> int:
        """Compatibility adapter: agent usage uses the generic daily grain."""
        return self.upsert_proxy_batch([
            {**row, "account_id": row["software_id"]} for row in rows
        ])

    def reconcile_agent_allocations(
            self, allocations: dict[tuple[int, str], dict[str, dict]],
            current_month: str) -> None:
        """Insert immutable agent allocations without rewriting old periods."""
        frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for (account_id, unit_id), periods in allocations.items():
            for month, values in periods.items():
                self.upsert_frozen_agent_allocation(
                    month=month, account_id=account_id,
                    billing_unit_id=unit_id,
                    recurring_charge=float(values.get("recurring_charge", 0) or 0),
                    normalized_recurring_cost=values.get("normalized_recurring_cost"),
                    currency=values.get("currency", "CNY"),
                    base_currency=values.get("base_currency", "CNY"),
                    fx_rate_date=values.get("fx_rate_date"),
                    frozen_at=values.get("finalized_at") or frozen_at)

    def purge_zero_agent_usage_rows(self) -> int:
        """Compatibility no-op after agent archive unification."""
        return 0

    def purge_zero_usage_rows(self) -> int:
        """Delete archive rows that carry no real usage.

        A (date, model, account_id) bucket with request/cost rows but NO
        token_usage rows represents failed/aborted or test requests — they are
        recorded with zero tokens (and zero cost) and must not show up as
        empty per-model cards. token_usage rows only exist for positive
        amounts, so "has token rows" == "has real usage"; buckets with any
        token usage (even 0-cost) are always preserved. Returns rows deleted.
        """
        conn = self._connect()
        try:
            deleted = conn.execute(
                "DELETE FROM daily_usage WHERE input_tokens=0 AND "
                "cache_tokens=0 AND output_tokens=0 AND equivalent_cost=0"
            ).rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    def accumulate_plan_summary(self, month: str, account_id: int,
                                billing_unit_id: str,
                                subscription_cost: float, virtual_cost: float,
                                refresh_subscription: bool = False):
        """Accumulate request-derived virtual cost without changing charges."""
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO monthly_recurring_costs
                   (month,account_id,billing_unit_id,recurring_charge,equivalent_cost,
                    normalized_recurring_cost,base_currency)
                   VALUES(?,?,?,0,?,0,'CNY')
                   ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
                     equivalent_cost=monthly_recurring_costs.equivalent_cost+
                                     excluded.equivalent_cost""",
                (month, account_id, billing_unit_id, virtual_cost),
            )
            conn.commit()
        finally:
            conn.close()

    def reconcile_plan_subscription(self, account_id: int, billing_unit_id: str,
                                    subscriptions: dict[str, float]) -> None:
        """Compatibility adapter for inserting already-confirmed CNY charges."""
        frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for month, cost in subscriptions.items():
            self.upsert_frozen_plan_charge(
                month=month, account_id=account_id,
                billing_unit_id=billing_unit_id,
                recurring_charge=float(cost or 0),
                normalized_recurring_cost=float(cost or 0),
                currency="CNY", base_currency="CNY", fx_rate_date=None,
                frozen_at=frozen_at)

    def record_billing_export_event(self, event: dict,
                                    payload_hash: str) -> int:
        """Apply one immutable billing event and retain its delivery receipt.

        The receipt is deliberately separate from the visible monthly row.
        Dashboard deletion removes the latter but keeps the receipt, so a
        later export from another machine cannot resurrect an old charge.
        """
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT payload_hash FROM billing_export_receipts "
                "WHERE event_key=?", (event["event_key"],)
            ).fetchone()
            if existing is not None:
                if existing[0] == "":
                    return 0
                if existing[0] != payload_hash:
                    raise ValueError(
                        f"billing event payload changed: {event['event_key']}")
                return 0

            legacy = conn.execute(
                """SELECT payload_hash FROM billing_export_receipts
                   WHERE event_key=?""",
                (f"legacy:{event['account_id']}:{event['month']}:{event['billing_unit_id']}",),
            ).fetchone()
            if legacy is not None:
                return 0

            if not (float(event["recurring_charge"] or 0) == 0 and
                    float(event["normalized_recurring_cost"] or 0) == 0):
                conn.execute(
                    """INSERT INTO monthly_recurring_costs
                       (month,account_id,billing_unit_id,recurring_charge,
                        equivalent_cost,currency,normalized_recurring_cost,
                        base_currency,fx_rate_date,charge_frozen_at)
                       VALUES(?,?,?,?,0,?,?,?,?,?)
                       ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
                         recurring_charge=excluded.recurring_charge,
                         currency=excluded.currency,
                         normalized_recurring_cost=excluded.normalized_recurring_cost,
                         base_currency=excluded.base_currency,
                         fx_rate_date=excluded.fx_rate_date,
                         charge_frozen_at=excluded.charge_frozen_at
                       WHERE monthly_recurring_costs.charge_frozen_at IS NULL""",
                    (event["month"], event["account_id"],
                     event["billing_unit_id"], event["recurring_charge"],
                     event["currency"], event["normalized_recurring_cost"],
                     event["base_currency"], event["fx_rate_date"],
                     event["frozen_at"]),
                )
            conn.execute(
                """INSERT INTO billing_export_receipts
                   (event_key,account_id,month,billing_unit_id,payload_hash)
                   VALUES(?,?,?,?,?)""",
                (event["event_key"], event["account_id"],
                 event["month"], event["billing_unit_id"], payload_hash),
            )
            conn.commit()
            return 1
        finally:
            conn.close()

    def cleanup_stale_subscription_units(self, account_id: int,
                                         active_unit_ids: set[str]) -> None:
        """Retained for callers that still name the old reconciliation step."""
        return None
