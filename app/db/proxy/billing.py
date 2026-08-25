"""Recurring-charge ledger materialization for the normalized V1 schema.

Subscription fees in a foreign currency (USD) are converted to CNY at the
rate on the billing period's start date (period_start), locked once
determined: a USD row is locked as soon as fx_rates has an exact row for
that date (fx_rate_date == period_start afterwards), and the rate is read
from that row forever — price changes re-multiply the same rate, and later
daily rate movements never touch it.  Missing historical rates are fetched
on demand via fx.ensure_rate(?date=period_start); on failure the nearest
stored rate is used as a provisional (unlocked) value retried on the next
materialization.  Only locked USD rows and CNY rows are finalized when their
period ends.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from app.core import sqlite_runtime
from app.core.time import (
    UTC, as_utc, billing_period, format_utc, next_billing_period,
    parse_runtime_timestamp, utc_now,
)
from app.db.proxy.common import _parse_iso_date
from app.services import fx
from app.services.billing_units import BillingUnitResolver


def _period(moment: datetime, anchor: int) -> tuple[datetime, datetime]:
    period = billing_period(moment, anchor)
    return period.start, period.end


def _as_utc_datetime(value: datetime | date) -> datetime:
    """Normalize a calendar date to a UTC datetime for period arithmetic."""
    return as_utc(value).replace(microsecond=0)


def _next_period(start: datetime, anchor: int) -> tuple[datetime, datetime]:
    period = next_billing_period(start, anchor)
    return period.start, period.end


def _rate(conn: sqlite3.Connection, contract_id: int, start: str,
          as_of: str) -> float:
    row = conn.execute(
        "SELECT recurring_price FROM billing_rate_events "
        "WHERE contract_id=? AND ((effective_rule='immediate' AND effective_at<=?) "
        "OR (effective_rule='next_period' AND effective_at<=?)) "
        "ORDER BY effective_at DESC,id DESC LIMIT 1",
        (contract_id, as_of, start),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _normalized_charge(conn: sqlite3.Connection, price: float,
                       currency: str, start_date: str,
                       attempted: set[tuple[str, str]] | None = None
                       ) -> tuple[float, str | None]:
    """Convert ``price`` to CNY at the rate locked on the period start date.

    Locking rule: a USD row is locked as soon as fx_rates has an exact row
    for *start_date* (fx_rate_date == start_date afterwards); the rate is
    then read from that row forever (price changes re-multiply the same
    rate).  When the exact row is missing, one best-effort historical fetch
    (?date=start_date) is attempted per run; on failure the nearest stored
    rate is used as a provisional (not locked) value and the row is retried
    on the next materialization.
    """
    if currency == "CNY":
        return price, None
    resolution = fx.FxRateResolver.resolve(conn, currency, "CNY", start_date)
    if resolution.locked:
        return price * fx.FxRateResolver.finalize(resolution), resolution.source_date
    key = (currency, start_date)
    if attempted is None or key not in attempted:
        resolution = fx.FxRateResolver.ensure(conn, currency, "CNY", start_date)
        if attempted is not None:
            attempted.add(key)
    if not resolution.locked:
        resolution = fx.FxRateResolver.resolve(conn, currency, "CNY", start_date)
    rate = resolution.rate
    return price * rate, resolution.source_date


def _rate_from_table(conn: sqlite3.Connection, table: str, owner_column: str,
                     owner_id: int, start: str, as_of: str) -> float:
    """Read a Plan or agent-instance rate using the same effective rules."""
    row = conn.execute(
        f"SELECT recurring_price FROM {table} "
        f"WHERE {owner_column}=? AND ((effective_rule='immediate' AND effective_at<=?) "
        f"OR (effective_rule='next_period' AND effective_at<=?)) "
        "ORDER BY effective_at DESC,id DESC LIMIT 1",
        (owner_id, as_of, start),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _materialize_period_stream(
        conn: sqlite3.Connection, *, owner_id: int, anchor: date,
        currency: str, valid_until: str | None, moment: datetime, now: str,
        rate_table: str, rate_owner_column: str, charge_table: str,
        charge_owner_column: str, credential_uuid: str | None,
        attempted: set[tuple[str, str]],
        charge_subscription_id: int | None = None) -> int:
    """Materialize one recurring stream using the shared Plan algorithm."""
    ended = None
    if valid_until:
        ended = parse_runtime_timestamp(valid_until)
        if ended is None:
            raise ValueError("billing timestamp cannot be empty")
        ended = ended.replace(microsecond=0)
    cutoff = min(moment, ended) if ended else moment
    anchor_day = anchor.day
    start_dt, end_dt = _period(_as_utc_datetime(anchor), anchor_day)
    changed = 0
    while start_dt <= cutoff:
        start, end = format_utc(start_dt), format_utc(end_dt)
        as_of = format_utc(min(moment, end_dt))
        price = _rate_from_table(
            conn, rate_table, rate_owner_column, owner_id, start, as_of)
        normalized, fx_date = _normalized_charge(
            conn, price, currency, start[:10], attempted)
        has_credential = charge_table == "billing_period_charges"
        if has_credential:
            existing = conn.execute(
                f"SELECT id,finalized_at FROM {charge_table} "
                f"WHERE {charge_owner_column}=? AND credential_uuid IS ? "
                "AND period_start=?",
                (owner_id, credential_uuid, start),
            ).fetchone()
        else:
            existing = conn.execute(
                f"SELECT id,finalized_at FROM {charge_table} "
                f"WHERE {charge_owner_column}=? AND period_start=?",
                (owner_id, start),
            ).fetchone()
        values = (price, currency, normalized, "CNY", fx_date)
        if existing is None:
            if has_credential:
                conn.execute(
                    f"INSERT INTO {charge_table}"
                    f"({charge_owner_column},credential_uuid,period_start,period_end,"
                    "recurring_charge,currency,normalized_recurring_cost,base_currency,fx_rate_date) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (owner_id, credential_uuid, start, end, *values),
                )
            else:
                conn.execute(
                    f"INSERT INTO {charge_table}"
                    f"({charge_owner_column},subscription_id,period_start,period_end,recurring_charge,currency,"
                    "normalized_recurring_cost,base_currency,fx_rate_date) VALUES(?,?,?,?,?,?,?,?,?)",
                    (owner_id, charge_subscription_id, start, end, *values),
                )
            changed += 1
        elif existing["finalized_at"] is None:
            conn.execute(
                f"UPDATE {charge_table} SET recurring_charge=?,currency=?,"
                "normalized_recurring_cost=?,base_currency=?,fx_rate_date=? WHERE id=?",
                (*values, existing["id"]),
            )
            changed += 1
        start_dt, end_dt = _next_period(start_dt, anchor_day)
    return changed


def _finalize_period_stream(conn: sqlite3.Connection, table: str,
                            now: str) -> int:
    finalized = conn.execute(
        f"UPDATE {table} SET finalized_at=? WHERE finalized_at IS NULL "
        "AND normalized_recurring_cost IS NOT NULL "
        "AND (currency='CNY' OR fx_rate_date=date(period_start)) "
        "AND period_end<=?", (now, now)
    )
    return max(finalized.rowcount, 0)


def materialize_period_charges(db_path: str,
                               at: datetime | None = None) -> int:
    """Create/update current charges idempotently and freeze closed periods."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    now = format_utc(moment)
    conn = sqlite_runtime.connect(db_path, "billing_write")
    changed = 0
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            attempted: set[tuple[str, str]] = set()
            for unit in BillingUnitResolver.proxy_units(conn, at=moment):
                changed += _materialize_period_stream(
                    conn, owner_id=unit.owner_id, anchor=unit.valid_from,
                    currency=unit.currency,
                    valid_until=BillingUnitResolver.end_stamp(unit), moment=moment,
                    now=now, rate_table="billing_rate_events",
                    rate_owner_column="contract_id",
                    charge_table="billing_period_charges",
                    charge_owner_column="contract_id",
                    credential_uuid=unit.credential_uuid, attempted=attempted)
            changed += _finalize_period_stream(conn, "billing_period_charges", now)
            return changed
    finally:
        conn.close()


def materialize_agent_subscription_charges(
        db_path: str, at: datetime | None = None) -> int:
    """Materialize agent subscription instances with Plan's exact rules."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    now = format_utc(moment)
    conn = sqlite_runtime.connect(db_path, "billing_write")
    changed = 0
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            attempted: set[tuple[str, str]] = set()
            for unit in BillingUnitResolver.agent_units(conn, at=moment):
                changed += _materialize_period_stream(
                    conn, owner_id=unit.owner_id, anchor=unit.valid_from,
                    currency=unit.currency, valid_until=BillingUnitResolver.end_stamp(unit),
                    moment=moment, now=now,
                    rate_table="agent_subscription_rate_events",
                    rate_owner_column="instance_id",
                    charge_table="agent_subscription_period_charges",
                    charge_owner_column="instance_id", credential_uuid=None,
                    charge_subscription_id=unit.subscription_id,
                    attempted=attempted)
            changed += _finalize_period_stream(
                conn, "agent_subscription_period_charges", now)
            return changed
    finally:
        conn.close()


def materialize_all_period_charges(db_path: str,
                                   at: datetime | None = None) -> int:
    """Materialize proxy Plans and agent subscription instances."""
    return (materialize_period_charges(db_path, at)
            + materialize_agent_subscription_charges(db_path, at))
