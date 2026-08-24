"""DashboardWriterMixin implementation."""

from app.db.dashboard.common import *  # noqa: F401,F403


class DashboardWriterMixin:
    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/dashboard/vN/*.sql);
        # apply once at construction. Fails fast (caller aborts) on error.
        # schema_dir may be passed explicitly when db_path is a shadow/temp
        # copy outside the standard data/ layout (schema_dir_for would mis-derive).
        from app.db.migrations import MigrationError, migrate, schema_dir_for
        migrate(self.db_path, schema_dir or schema_dir_for(self.db_path, "dashboard"),
                "dashboard")
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT major FROM schema_version WHERE id=1").fetchone()
        finally:
            conn.close()
        if not row or int(row[0]) != 1:
            raise MigrationError(
                "DashboardDatabase only accepts V1; upgrade the database before "
                "opening the application")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

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
        """Reconcile current/future actual subscription shares per agent.

        ``equivalent_cost`` is intentionally untouched: it is the agent's
        theoretical token cost from daily usage.  Only recurring/native and
        normalized actual fields are changed here.
        """
        conn = self._connect()
        try:
            # A deleted software has no active allocation, but its already
            # materialized share for the current month remains payable.  Keep
            # that row until another software becomes bound to the same
            # subscription; then the fresh allocation replaces the stale one.
            conn.execute(
                "UPDATE monthly_recurring_costs SET recurring_charge=0,"
                "normalized_recurring_cost=0 "
                "WHERE month>? AND billing_unit_id LIKE 'agent-subscription:%' "
                "AND account_id IN (SELECT account_id FROM accounts WHERE account_kind='agent')",
                (current_month,),
            )
            active_units = sorted({unit_id for (_account_id, unit_id) in allocations})
            if active_units:
                placeholders = ",".join("?" for _ in active_units)
                conn.execute(
                    "UPDATE monthly_recurring_costs SET recurring_charge=0,"
                    "normalized_recurring_cost=0 "
                    f"WHERE month=? AND billing_unit_id IN ({placeholders}) "
                    "AND account_id IN (SELECT account_id FROM accounts WHERE account_kind='agent')",
                    (current_month, *active_units),
                )
            for (account_id, unit_id), periods in allocations.items():
                for month, values in periods.items():
                    conn.execute(
                        "INSERT INTO monthly_recurring_costs"
                        "(month,account_id,billing_unit_id,recurring_charge,equivalent_cost,"
                        "currency,normalized_recurring_cost,base_currency,fx_rate_date) "
                        "VALUES(?,?,?,?,0,?,?,?,?) "
                        "ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET "
                        "recurring_charge=excluded.recurring_charge,"
                        "currency=excluded.currency,"
                        "normalized_recurring_cost=excluded.normalized_recurring_cost,"
                        "base_currency=excluded.base_currency,fx_rate_date=excluded.fx_rate_date",
                        (month, account_id, unit_id,
                         values.get("recurring_charge", 0),
                         values.get("currency", "CNY"),
                         values.get("normalized_recurring_cost"),
                         values.get("base_currency", "CNY"),
                         values.get("fx_rate_date")),
                    )
            conn.commit()
        finally:
            conn.close()

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
        """Accumulate one recurring contract's monthly economics.

        virtual_cost is ADDED to the bucket: request_log rows are cleaned 30d
        after export, so the archive can never be recomputed from logs — it
        accumulates (each export batch contributes once). subscription_cost is
        set on first write and refreshed ONLY while the month is still current
        (refresh_subscription=True), so past months are frozen forever — price
        edits affect only the current month.
        """
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO monthly_recurring_costs
                   (month,account_id,billing_unit_id,recurring_charge,equivalent_cost,
                    normalized_recurring_cost,base_currency)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
                     equivalent_cost=monthly_recurring_costs.equivalent_cost+
                                     excluded.equivalent_cost,
                     recurring_charge=CASE WHEN ? THEN excluded.recurring_charge
                       ELSE monthly_recurring_costs.recurring_charge END,
                     normalized_recurring_cost=CASE WHEN ? THEN
                       excluded.normalized_recurring_cost ELSE
                       monthly_recurring_costs.normalized_recurring_cost END""",
                (month, account_id, billing_unit_id, subscription_cost, virtual_cost,
                 subscription_cost, "CNY", bool(refresh_subscription),
                 bool(refresh_subscription)),
            )
            conn.commit()
        finally:
            conn.close()

    def reconcile_plan_subscription(self, account_id: int, billing_unit_id: str,
                                    subscriptions: dict[str, float]) -> None:
        """Set one key lifecycle's subscription rows without touching virtual cost.

        Unlike request usage, subscriptions are derived state.  Reconciliation
        lets an edited valid_from, cancellation, or scheduled price remove a
        previously generated period while preserving the additive virtual-cost
        archive in the same row.
        """
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT month FROM monthly_recurring_costs "
                "WHERE account_id=? AND billing_unit_id=?",
                (account_id, billing_unit_id),
            ).fetchall()
            for row in existing:
                if row["month"] not in subscriptions:
                    conn.execute(
                        "UPDATE monthly_recurring_costs SET recurring_charge=0,"
                        "normalized_recurring_cost=0 "
                        "WHERE account_id=? AND billing_unit_id=? AND month=?",
                        (account_id, billing_unit_id, row["month"]),
                    )
            for month, cost in subscriptions.items():
                conn.execute(
                    "INSERT INTO monthly_recurring_costs"
                    "(month,account_id,billing_unit_id,recurring_charge,equivalent_cost,"
                    "normalized_recurring_cost,base_currency) VALUES(?,?,?,?,0,?,?) "
                    "ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET "
                    "recurring_charge=excluded.recurring_charge,"
                    "normalized_recurring_cost=excluded.normalized_recurring_cost",
                    (month, account_id, billing_unit_id, cost, cost, "CNY"),
                )
            conn.commit()
        finally:
            conn.close()

    def cleanup_stale_subscription_units(self, account_id: int,
                                         active_unit_ids: set[str]) -> None:
        """Zero subscription rows for units no longer in the lifecycle set.

        V0→V1 migrations could carry rows under legacy billing-unit ids (for
        example a per-account row keyed by a synthesized credential uuid while
        the live export keys it as ``contract:<uuid>``).  Those rows must keep
        their additive virtual-cost archive, but their subscription portion
        must not be counted twice after the export starts reconciling the
        canonical unit ids.
        """
        conn = self._connect()
        try:
            if active_unit_ids:
                conn.execute(
                    "UPDATE monthly_recurring_costs SET recurring_charge=0,"
                    "normalized_recurring_cost=0 "
                    "WHERE account_id=? AND billing_unit_id NOT IN (%s)"
                    % ",".join("?" for _ in active_unit_ids),
                    (account_id, *sorted(active_unit_ids)),
                )
            else:
                conn.execute(
                    "UPDATE monthly_recurring_costs SET recurring_charge=0,"
                    "normalized_recurring_cost=0 WHERE account_id=?",
                    (account_id,),
                )
            conn.commit()
        finally:
            conn.close()
