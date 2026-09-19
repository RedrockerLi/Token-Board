"""Write operations for the two-table Dashboard archive."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from app.core import sqlite_runtime


def _day(value: object) -> str:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def _micro(value: object) -> int:
    """Convert CNY to integer micro-CNY with half-up rounding."""
    if value is None:
        return 0
    return int((Decimal(str(value)) * Decimal(1_000_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


class DashboardWriterMixin:
    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        from app.db.migrations import schema_dir_for
        from app.db.schema_upgrade import verify_current_database
        self.schema_dir = schema_dir or schema_dir_for(self.db_path, "dashboard")
        verify_current_database(self.db_path, "dashboard", self.schema_dir)

    def _connect(self) -> sqlite3.Connection:
        return sqlite_runtime.connect(self.db_path, "dashboard_runtime")

    @staticmethod
    def _user_name(row: dict) -> str:
        name = str(row.get("name", row.get("account_name", "")) or "").strip()
        return name or f"用户 {int(row['account_id'])}"

    @staticmethod
    def _ensure_user(conn: sqlite3.Connection, user_id: int, name: str) -> None:
        user_id = int(user_id)
        if user_id < 1:
            raise ValueError("real Dashboard user_id must be greater than zero")
        conn.execute(
            """INSERT INTO users(id,name,actual_cost_micro_cny) VALUES(?,?,0)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
            (user_id, name),
        )

    def upsert_account_batch(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                user_id = int(row.get("user_id", row.get("account_id")))
                self._ensure_user(
                    conn, user_id,
                    self._user_name({**row, "account_id": user_id}),
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def archive_users(self, user_ids: set[int] | list[int] | tuple[int, ...]) -> int:
        """Merge ordinary users into id=0 and remove their source identity."""
        raw_ids = {int(value) for value in (user_ids or [])}
        if 0 in raw_ids:
            raise ValueError("archive user_id=0 cannot be archived")
        ids = sorted(raw_ids)
        if any(value < 0 for value in ids):
            raise ValueError("user_id must be non-negative")
        if not ids:
            return 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            archived_rows = 0
            for user_id in ids:
                exists = conn.execute(
                    "SELECT 1 FROM users WHERE id=? AND id<>0", (user_id,)
                ).fetchone()
                if exists is None:
                    continue
                archived_rows += conn.execute(
                    "SELECT COUNT(*) FROM daily_model_usage WHERE user_id=?",
                    (user_id,),
                ).fetchone()[0]
                conn.execute(
                    """INSERT INTO daily_model_usage(
                           user_id,usage_date,model,input_tokens,cache_read_tokens,
                           output_tokens,request_count,api_equivalent_cost_micro_cny)
                       SELECT 0,usage_date,model,SUM(input_tokens),
                              SUM(cache_read_tokens),SUM(output_tokens),
                              SUM(request_count),SUM(api_equivalent_cost_micro_cny)
                         FROM daily_model_usage WHERE user_id=?
                        GROUP BY usage_date,model
                       ON CONFLICT(user_id,usage_date,model) DO UPDATE SET
                           input_tokens=daily_model_usage.input_tokens+excluded.input_tokens,
                           cache_read_tokens=daily_model_usage.cache_read_tokens+excluded.cache_read_tokens,
                           output_tokens=daily_model_usage.output_tokens+excluded.output_tokens,
                           request_count=daily_model_usage.request_count+excluded.request_count,
                           api_equivalent_cost_micro_cny=
                             daily_model_usage.api_equivalent_cost_micro_cny+
                             excluded.api_equivalent_cost_micro_cny""",
                    (user_id,),
                )
                conn.execute(
                    """UPDATE users SET actual_cost_micro_cny=
                           actual_cost_micro_cny +
                           (SELECT actual_cost_micro_cny FROM users WHERE id=?)
                       WHERE id=0""",
                    (user_id,),
                )
                conn.execute("DELETE FROM daily_model_usage WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            conn.commit()
            return archived_rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Compatibility name used by older internal callers; it now archives.
    def purge_accounts(self, account_ids):
        return self.archive_users(account_ids)

    def _upsert_usage_rows(self, conn: sqlite3.Connection, rows: list[dict]) -> int:
        for row in rows:
            user_id = int(row.get("user_id", row.get("account_id")))
            supplied_name = row.get("name", row.get("account_name"))
            if supplied_name is not None and str(supplied_name).strip():
                self._ensure_user(conn, user_id, self._user_name({**row, "account_id": user_id}))
            elif conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
                self._ensure_user(conn, user_id, f"用户 {user_id}")
            eq_micro = row.get("api_equivalent_cost_micro_cny")
            if eq_micro is None:
                eq_micro = _micro(row.get("cost", 0))
            billed_micro = row.get("actual_cost_micro_cny")
            if billed_micro is None:
                billed_micro = _micro(row.get("billed_usage_cost", 0))
            conn.execute(
                """INSERT INTO daily_model_usage(
                       user_id,usage_date,model,input_tokens,cache_read_tokens,
                       output_tokens,request_count,api_equivalent_cost_micro_cny)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,usage_date,model) DO UPDATE SET
                       input_tokens=daily_model_usage.input_tokens+excluded.input_tokens,
                       cache_read_tokens=daily_model_usage.cache_read_tokens+excluded.cache_read_tokens,
                       output_tokens=daily_model_usage.output_tokens+excluded.output_tokens,
                       request_count=daily_model_usage.request_count+excluded.request_count,
                       api_equivalent_cost_micro_cny=
                         daily_model_usage.api_equivalent_cost_micro_cny+
                         excluded.api_equivalent_cost_micro_cny""",
                (user_id, _day(row.get("usage_date", row.get("date"))),
                 str(row["model"]),
                 int(row.get("input_tokens", row.get("prompt_tokens", 0)) or 0),
                 int(row.get("cache_read_tokens", row.get("cache_tokens", 0)) or 0),
                 int(row.get("output_tokens", row.get("completion_tokens", 0)) or 0),
                 int(row.get("request_count", 0) or 0), int(eq_micro)),
            )
            if billed_micro:
                conn.execute(
                    """UPDATE users SET actual_cost_micro_cny=
                           actual_cost_micro_cny+? WHERE id=?""",
                    (int(billed_micro), user_id),
                )
        return len(rows)

    def upsert_proxy_data(self, date: str, model: str, account_id: int,
                          prompt_tokens: int, completion_tokens: int,
                          cache_read_tokens: int, request_count: int,
                          cost: float = 0.0,
                          billed_usage_cost: float | None = None) -> int:
        return self.upsert_proxy_batch([{
            "date": date, "model": model, "account_id": account_id,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "cache_read_tokens": cache_read_tokens, "request_count": request_count,
            "cost": cost,
            "billed_usage_cost": cost if billed_usage_cost is None else billed_usage_cost,
        }])

    def upsert_proxy_batch(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = self._upsert_usage_rows(conn, rows)
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _add_actual_cost(self, account_id: int, name: str, amount: object) -> int:
        amount_micro = _micro(amount)
        if amount_micro == 0:
            return 0
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM users WHERE id=?", (int(account_id),)
            ).fetchone()
            if existing is None:
                self._ensure_user(conn, int(account_id), name)
            conn.execute(
                "UPDATE users SET actual_cost_micro_cny=actual_cost_micro_cny+? WHERE id=?",
                (amount_micro, int(account_id)),
            )
            conn.commit()
            return 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_frozen_plan_charge(self, *, period_start: str | None = None,
                                  month: str | None = None, account_id: int,
                                  billing_unit_id: str, recurring_charge: float,
                                  normalized_recurring_cost: float | None,
                                  currency: str = "CNY",
                                  base_currency: str = "CNY",
                                  fx_rate_date: str | None = None,
                                  frozen_on: str,
                                  account_name: str | None = None) -> int:
        if normalized_recurring_cost is None:
            return 0
        return self._add_actual_cost(
            account_id, account_name or f"用户 {int(account_id)}",
            normalized_recurring_cost)

    def upsert_frozen_agent_allocation(self, **kwargs) -> int:
        return self.upsert_frozen_plan_charge(**kwargs)

    def upsert_agent_software(self, rows: list[dict]) -> int:
        return self.upsert_account_batch([
            {"account_id": row["software_id"], "name": row["name"]}
            for row in rows
        ])

    def upsert_agent_batch(self, rows: list[dict]) -> int:
        return self.upsert_proxy_batch([
            {**row, "account_id": row["software_id"]} for row in rows
        ])

    def reconcile_agent_allocations(self, allocations, current_month: str) -> None:
        for (account_id, _unit_id), periods in allocations.items():
            for month, values in periods.items():
                self.upsert_frozen_plan_charge(
                    account_id=account_id, billing_unit_id="agent",
                    recurring_charge=values.get("recurring_charge", 0),
                    normalized_recurring_cost=values.get("normalized_recurring_cost"),
                    frozen_on=values.get("finalized_on") or str(month)[:10],
                )

    def purge_zero_agent_usage_rows(self) -> int:
        return 0

    def purge_zero_usage_rows(self) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                """DELETE FROM daily_model_usage
                   WHERE input_tokens=0 AND cache_read_tokens=0
                     AND output_tokens=0 AND request_count=0
                     AND api_equivalent_cost_micro_cny=0"""
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def accumulate_plan_summary(self, **kwargs):
        """Compatibility no-op: plan/agent cost is already in daily usage."""
        return 0

    def reconcile_plan_subscription(self, account_id: int, billing_unit_id: str,
                                    subscriptions: dict[str, float]) -> None:
        for month, cost in subscriptions.items():
            self.upsert_frozen_plan_charge(
                account_id=account_id, billing_unit_id=billing_unit_id,
                recurring_charge=float(cost or 0),
                normalized_recurring_cost=float(cost or 0),
                frozen_on=f"{str(month)[:7]}-01")

    def record_billing_export_event(self, event: dict, payload_hash: str) -> int:
        if event.get("normalized_recurring_cost") is None:
            return 0
        return self._add_actual_cost(
            int(event["account_id"]),
            str(event.get("account_name") or f"用户 {int(event['account_id'])}"),
            event.get("normalized_recurring_cost"),
        )

    def cleanup_stale_subscription_units(self, account_id: int,
                                         active_unit_ids: set[str]) -> None:
        return None
