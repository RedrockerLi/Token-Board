"""Recurring-charge ledger materialization for the normalized V1 schema.

Subscription fees in a foreign currency (USD) are converted to CNY at the
rate on the billing period's start date (period_start), locked once
determined: a USD row is locked as soon as fx_rates has an exact row for
that date (fx_rate_date == period_start afterwards), and the rate is read
from that row forever — price changes re-multiply the same rate, and later
daily rate movements never touch it.  Missing historical rates are fetched
on demand via fx.ensure_rate(?date=period_start); on failure the nearest
stored rate is frozen as the permanent fallback.  Recurring rows are finalized
as soon as their period-start amount is known.
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
    """Read the rate effective at the beginning of a billing period."""
    row = conn.execute(
        "SELECT recurring_price FROM billing_rate_events "
        "WHERE contract_id=? AND effective_at<=? "
        "ORDER BY effective_at DESC,id DESC LIMIT 1",
        (contract_id, start),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _normalized_charge(conn: sqlite3.Connection, price: float,
                       currency: str, start_date: str,
                       attempted: set[tuple[str, str]] | None = None
                       ) -> tuple[float | None, str | None, bool]:
    """Convert ``price`` to CNY at the rate locked on the period start date.

    A USD row is locked using the exact period-start rate when available. If
    it is missing, one best-effort historical fetch is attempted and the
    nearest stored rate becomes the permanent fallback. A currency with no
    stored rate at all remains pending instead of silently using 1.0.
    """
    if currency == "CNY":
        return price, None, True
    resolution = fx.FxRateResolver.resolve(conn, currency, "CNY", start_date)
    if not resolution.exact:
        key = (currency, start_date)
        if attempted is None or key not in attempted:
            resolution = fx.FxRateResolver.ensure(conn, currency, "CNY", start_date)
            if attempted is not None:
                attempted.add(key)
        else:
            resolution = fx.FxRateResolver.resolve(conn, currency, "CNY", start_date)
    if resolution.source_date is None:
        return None, None, False
    return price * resolution.rate, resolution.source_date, True


def _rate_from_table(conn: sqlite3.Connection, table: str, owner_column: str,
                     owner_id: int, start: str, as_of: str) -> float:
    """Read the rate that was effective when a period started."""
    row = conn.execute(
        f"SELECT recurring_price FROM {table} "
        f"WHERE {owner_column}=? AND effective_at<=? "
        "ORDER BY effective_at DESC,id DESC LIMIT 1",
        (owner_id, start),
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
        has_credential = charge_table == "billing_period_charges"
        if has_credential:
            existing = conn.execute(
                f"SELECT id,finalized_at,recurring_charge,currency,"
                f"normalized_recurring_cost,fx_rate_date FROM {charge_table} "
                f"WHERE {charge_owner_column}=? AND credential_uuid IS ? "
                "AND period_start=?",
                (owner_id, credential_uuid, start),
            ).fetchone()
        else:
            existing = conn.execute(
                f"SELECT id,finalized_at,recurring_charge,currency,"
                f"normalized_recurring_cost,fx_rate_date FROM {charge_table} "
                f"WHERE {charge_owner_column}=? AND period_start=?",
                (owner_id, start),
            ).fetchone()
        # A finalized row is a financial fact. Do not even resolve its rate
        # again: this avoids network access for permanently-fallback FX rows
        # and guarantees that later price/rate events cannot rewrite history.
        if existing is not None and existing["finalized_at"] is not None:
            start_dt, end_dt = _next_period(start_dt, anchor_day)
            continue

        if existing is not None:
            # The migration owns the one-time correction of legacy open rows.
            # Runtime materialization may only fill missing FX fields; it must
            # never replace the recorded native price or currency.
            price = float(existing["recurring_charge"] or 0)
            row_currency = existing["currency"] or currency
            if existing["normalized_recurring_cost"] is not None:
                normalized, fx_date, can_finalize = (
                    float(existing["normalized_recurring_cost"]),
                    existing["fx_rate_date"], True)
            else:
                normalized, fx_date, can_finalize = _normalized_charge(
                    conn, price, row_currency, start[:10], attempted)
            currency = row_currency
        else:
            price = _rate_from_table(
                conn, rate_table, rate_owner_column, owner_id, start, start)
            normalized, fx_date, can_finalize = _normalized_charge(
                conn, price, currency, start[:10], attempted)
        values = (price, currency, normalized, "CNY", fx_date)
        if existing is None:
            if has_credential:
                conn.execute(
                    f"INSERT INTO {charge_table}"
                    f"({charge_owner_column},credential_uuid,period_start,period_end,"
                    "recurring_charge,currency,normalized_recurring_cost,base_currency,fx_rate_date,finalized_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (owner_id, credential_uuid, start, end, *values,
                     now if can_finalize else None),
                )
            else:
                conn.execute(
                    f"INSERT INTO {charge_table}"
                    f"({charge_owner_column},subscription_id,period_start,period_end,recurring_charge,currency,"
                    "normalized_recurring_cost,base_currency,fx_rate_date,finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (owner_id, charge_subscription_id, start, end, *values,
                     now if can_finalize else None),
                )
            changed += 1
        elif existing["finalized_at"] is None:
            if can_finalize:
                conn.execute(
                    f"UPDATE {charge_table} SET normalized_recurring_cost=?,"
                    "base_currency=?,fx_rate_date=?,finalized_at=? WHERE id=?",
                    (normalized, "CNY", fx_date, now, existing["id"]),
                )
                changed += 1
        start_dt, end_dt = _next_period(start_dt, anchor_day)
    return changed


def _finalize_period_stream(conn: sqlite3.Connection, table: str,
                            now: str) -> int:
    finalized = conn.execute(
        f"UPDATE {table} SET finalized_at=? WHERE finalized_at IS NULL "
        "AND normalized_recurring_cost IS NOT NULL "
        "AND (currency='CNY' OR fx_rate_date IS NOT NULL)", (now,)
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
                changed += _materialize_agent_charge_allocations(
                    conn, unit.owner_id, unit.account_id, unit.valid_from,
                    unit.subscription_id, now)
            changed += _finalize_period_stream(
                conn, "agent_subscription_period_charges", now)
            changed += _materialize_all_agent_charge_allocations(conn, now)
            return changed
    finally:
        conn.close()


def materialize_all_period_charges(db_path: str,
                                   at: datetime | None = None) -> int:
    """Materialize proxy Plans and agent subscription instances."""
    return (materialize_period_charges(db_path, at)
            + materialize_agent_subscription_charges(db_path, at))


def _materialize_agent_charge_allocations(
        conn: sqlite3.Connection, instance_id: int, account_id: int | None,
        valid_from: date, subscription_id: int | None, now: str) -> int:
    """Snapshot active software bindings for finalized instance charges."""
    if subscription_id is None:
        return 0
    charges = conn.execute(
        "SELECT id,period_start,recurring_charge,normalized_recurring_cost,"
        "currency,base_currency,fx_rate_date,finalized_at "
        "FROM agent_subscription_period_charges "
        "WHERE instance_id=? AND finalized_at IS NOT NULL",
        (instance_id,),
    ).fetchall()
    changed = 0
    for charge in charges:
        existing = conn.execute(
            "SELECT 1 FROM agent_subscription_charge_allocations "
            "WHERE period_charge_id=? LIMIT 1", (charge["id"],)
        ).fetchone()
        if existing is not None:
            continue
        bindings = conn.execute(
            "SELECT software_id FROM agent_subscription_bindings "
            "WHERE subscription_id=? AND lifecycle_state='active' "
            "AND valid_from<=? AND (valid_until IS NULL OR valid_until>?) "
            "ORDER BY software_id",
            (subscription_id, charge["period_start"], charge["period_start"]),
        ).fetchall()
        if not bindings:
            continue
        denominator = len(bindings)
        recurring = float(charge["recurring_charge"] or 0) / denominator
        normalized = (float(charge["normalized_recurring_cost"] or 0) / denominator
                      if charge["normalized_recurring_cost"] is not None else None)
        conn.executemany(
            "INSERT OR IGNORE INTO agent_subscription_charge_allocations"
            "(period_charge_id,software_id,recurring_charge,normalized_recurring_cost,"
            "currency,base_currency,fx_rate_date,finalized_at) VALUES(?,?,?,?,?,?,?,?)",
            [(charge["id"], row["software_id"], recurring, normalized,
              charge["currency"], charge["base_currency"], charge["fx_rate_date"],
              charge["finalized_at"]) for row in bindings],
        )
        changed += denominator
    return changed


def _materialize_all_agent_charge_allocations(
        conn: sqlite3.Connection, now: str) -> int:
    """Backfill allocations for finalized rows created before V1.12."""
    changed = 0
    instances = conn.execute(
        "SELECT i.id,i.valid_from,i.subscription_id "
        "FROM agent_subscription_instances i"
    ).fetchall()
    for instance in instances:
        changed += _materialize_agent_charge_allocations(
            conn, instance["id"], None,
            _parse_iso_date(str(instance["valid_from"])[:10]) or utc_now().date(),
            instance["subscription_id"], now)
    return changed
