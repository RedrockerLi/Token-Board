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
        """Write only identities involved in the current fact batch."""
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.executemany(
                """INSERT INTO accounts
                   (account_id,name,updated_at,account_kind)
                   VALUES(:account_id,:name,:updated_at,:account_kind)
                   ON CONFLICT(account_id) DO UPDATE SET
                     name=excluded.name,
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

    def upsert_frozen_plan_charge(self, *, period_start: str | None = None,
                                  month: str | None = None, account_id: int,
                                  billing_unit_id: str, recurring_charge: float,
                                  normalized_recurring_cost: float | None,
                                  currency: str, base_currency: str,
                                  fx_rate_date: str | None,
                                  frozen_at: str) -> int:
        """Insert one immutable recurring charge, updating only an unfrozen row."""
        if (recurring_charge == 0 and (normalized_recurring_cost or 0) == 0):
            return 0
        period_start = period_start or (
            month if month and "T" in month else f"{month}-01T00:00:00Z")
        conn = self._connect()
        try:
            cursor = conn.execute(
                """INSERT INTO monthly_recurring_costs
                   (period_start,account_id,billing_unit_id,recurring_charge,equivalent_cost,
                    currency,normalized_recurring_cost,base_currency,fx_rate_date,
                    charge_frozen_at)
                   VALUES(?,?,?,?,0,?,?,?,?,?)
                   ON CONFLICT(period_start,account_id,billing_unit_id) DO UPDATE SET
                     recurring_charge=excluded.recurring_charge,
                     currency=excluded.currency,
                     normalized_recurring_cost=excluded.normalized_recurring_cost,
                     base_currency=excluded.base_currency,
                     fx_rate_date=excluded.fx_rate_date,
                     charge_frozen_at=excluded.charge_frozen_at
                   WHERE monthly_recurring_costs.charge_frozen_at IS NULL""",
                (period_start, account_id, billing_unit_id, recurring_charge, currency,
                 normalized_recurring_cost, base_currency, fx_rate_date, frozen_at),
            )
            conn.commit()
            return max(cursor.rowcount, 0)
        finally:
            conn.close()

    def upsert_frozen_agent_allocation(self, *, period_start: str | None = None,
                                       month: str | None = None, account_id: int,
                                       billing_unit_id: str,
                                       recurring_charge: float,
                                       normalized_recurring_cost: float | None,
                                       currency: str, base_currency: str,
                                       fx_rate_date: str | None,
                                       frozen_at: str) -> int:
        return self.upsert_frozen_plan_charge(
            period_start=period_start, month=month, account_id=account_id,
            billing_unit_id=billing_unit_id,
            recurring_charge=recurring_charge,
            normalized_recurring_cost=normalized_recurring_cost,
            currency=currency, base_currency=base_currency,
            fx_rate_date=fx_rate_date, frozen_at=frozen_at)

    def upsert_agent_software(self, rows: list[dict]) -> int:
        """Compatibility adapter: agent names live in the generic mirror."""
        return self.upsert_account_batch([
            {"account_id": row["software_id"], "name": row["name"],
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
                    period_start=(month if "T" in str(month)
                                  else f"{month}-01T00:00:00Z"),
                    account_id=account_id,
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

    def accumulate_plan_summary(self, period_start: str | None = None,
                                account_id: int = 0,
                                billing_unit_id: str = "",
                                subscription_cost: float = 0,
                                virtual_cost: float = 0,
                                refresh_subscription: bool = False,
                                month: str | None = None):
        """Accumulate request-derived virtual cost without changing charges."""
        period_start = period_start or (
            month if month and "T" in month else f"{month}-01T00:00:00Z")
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO monthly_recurring_costs
                   (period_start,account_id,billing_unit_id,recurring_charge,equivalent_cost,
                    normalized_recurring_cost,base_currency)
                   VALUES(?,?,?,0,?,0,'CNY')
                   ON CONFLICT(period_start,account_id,billing_unit_id) DO UPDATE SET
                     equivalent_cost=monthly_recurring_costs.equivalent_cost+
                                     excluded.equivalent_cost""",
                (period_start, account_id, billing_unit_id, virtual_cost),
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
                period_start=f"{month}-01T00:00:00Z", account_id=account_id,
                billing_unit_id=billing_unit_id,
                recurring_charge=float(cost or 0),
                normalized_recurring_cost=float(cost or 0),
                currency="CNY", base_currency="CNY", fx_rate_date=None,
                frozen_at=frozen_at)

    def record_billing_export_event(self, event: dict,
                                    payload_hash: str) -> int:
        """Apply one immutable billing event by its natural fact key."""
        if (float(event.get("recurring_charge") or 0) == 0 and
                float(event.get("normalized_recurring_cost") or 0) == 0):
            # A zero-valued finalized source fact is retained in Token-Board,
            # but it is not a Dashboard fact and must not create an empty
            # account identity.
            return 0
        conn = self._connect()
        try:
            account = conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (event["account_id"],)
            ).fetchone()
            if account is None:
                conn.execute(
                    "INSERT INTO accounts(account_id,name,updated_at,account_kind) "
                    "VALUES(?,?,?,?)",
                    (event["account_id"], event["account_name"],
                     event["frozen_at"], event["account_kind"]),
                )
            existing = conn.execute(
                "SELECT recurring_charge,normalized_recurring_cost,currency,"
                "base_currency,fx_rate_date,charge_frozen_at FROM monthly_recurring_costs "
                "WHERE period_start=? AND account_id=? AND billing_unit_id=?",
                (event["period_start"], event["account_id"], event["billing_unit_id"]),
            ).fetchone()
            if existing is None:
                # V1 Dashboard rows were month-grained.  On the first V2
                # event, move that provisional month row to the immutable
                # period_start rather than creating a second charge for the
                # same account/unit.
                legacy_period = f"{event['month']}-01T00:00:00Z"
                if legacy_period != event["period_start"]:
                    legacy = conn.execute(
                        "SELECT recurring_charge,normalized_recurring_cost,currency,"
                        "base_currency,fx_rate_date,charge_frozen_at FROM "
                        "monthly_recurring_costs WHERE period_start=? AND account_id=? "
                        "AND billing_unit_id=?",
                        (legacy_period, event["account_id"], event["billing_unit_id"]),
                    ).fetchone()
                    if legacy is not None:
                        conn.execute(
                            "UPDATE monthly_recurring_costs SET period_start=? "
                            "WHERE period_start=? AND account_id=? AND billing_unit_id=?",
                            (event["period_start"], legacy_period,
                             event["account_id"], event["billing_unit_id"]),
                        )
                        existing = legacy
            if existing is not None:
                current = (existing[0], existing[1], existing[2], existing[3],
                           existing[4], existing[5])
                expected = (event["recurring_charge"], event["normalized_recurring_cost"],
                            event["currency"], event["base_currency"],
                            event["fx_rate_date"], event["frozen_at"])
                # An older request-log export may have created a provisional
                # row for this natural key.  The immutable billing event is
                # allowed to freeze that row once; after freezing, a payload
                # change is a data conflict rather than an additive update.
                if existing[5] is None:
                    conn.execute(
                        "UPDATE monthly_recurring_costs SET recurring_charge=?,"
                        "currency=?,normalized_recurring_cost=?,base_currency=?,"
                        "fx_rate_date=?,charge_frozen_at=? WHERE period_start=? AND "
                        "account_id=? AND billing_unit_id=?",
                        (event["recurring_charge"], event["currency"],
                         event["normalized_recurring_cost"], event["base_currency"],
                        event["fx_rate_date"], event["frozen_at"], event["period_start"],
                         event["account_id"], event["billing_unit_id"]),
                    )
                    conn.commit()
                    return 1
                if any(current[index] != expected[index] for index in range(6)):
                    raise ValueError(
                        f"billing event payload changed: {event['event_key']}")
                return 0

            if not (float(event["recurring_charge"] or 0) == 0 and
                    float(event["normalized_recurring_cost"] or 0) == 0):
                conn.execute(
                    """INSERT INTO monthly_recurring_costs
                       (period_start,account_id,billing_unit_id,recurring_charge,
                        equivalent_cost,currency,normalized_recurring_cost,
                        base_currency,fx_rate_date,charge_frozen_at)
                       VALUES(?,?,?,?,0,?,?,?,?,?)
                       ON CONFLICT(period_start,account_id,billing_unit_id) DO UPDATE SET
                         recurring_charge=excluded.recurring_charge,
                         currency=excluded.currency,
                         normalized_recurring_cost=excluded.normalized_recurring_cost,
                         base_currency=excluded.base_currency,
                         fx_rate_date=excluded.fx_rate_date,
                         charge_frozen_at=excluded.charge_frozen_at
                       WHERE monthly_recurring_costs.charge_frozen_at IS NULL""",
                    (event["period_start"], event["account_id"],
                     event["billing_unit_id"], event["recurring_charge"],
                     event["currency"], event["normalized_recurring_cost"],
                     event["base_currency"], event["fx_rate_date"],
                     event["frozen_at"]),
                )
            conn.commit()
            return 1
        finally:
            conn.close()

    def cleanup_stale_subscription_units(self, account_id: int,
                                         active_unit_ids: set[str]) -> None:
        """Retained for callers that still name the old reconciliation step."""
        return None
