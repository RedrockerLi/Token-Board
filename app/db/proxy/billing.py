"""Recurring-charge ledger materialization for the normalized V2 schema.

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
from app.db.proxy.billing_export import (
    append_billing_export_events_for_finalized_at,
)
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


def _current_period_start(moment: datetime, anchor: int) -> str:
    return format_utc(billing_period(moment, anchor).start)


def proxy_billing_ready(conn: sqlite3.Connection, account_id: int,
                        at: datetime | None = None) -> bool:
    """Whether every currently billable proxy unit has a charge row."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    units = [u for u in BillingUnitResolver.proxy_units(conn, at=moment)
             if u.account_id == account_id]
    for unit in units:
        start = _current_period_start(moment, unit.anchor_day)
        row = conn.execute(
            "SELECT 1 FROM billing_period_charges WHERE contract_id=? "
            "AND credential_uuid IS ? AND period_start=? LIMIT 1",
            (unit.contract_id, unit.credential_uuid, start)).fetchone()
        if row is None:
            return False
    return True


def agent_billing_ready(conn: sqlite3.Connection, subscription_id: int,
                        at: datetime | None = None) -> bool:
    """Whether every currently billable instance has a charge row."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    units = [u for u in BillingUnitResolver.agent_units(conn, at=moment)
             if u.subscription_id == subscription_id]
    for unit in units:
        start = _current_period_start(moment, unit.anchor_day)
        row = conn.execute(
            "SELECT 1 FROM agent_subscription_period_charges "
            "WHERE instance_id=? AND period_start=? LIMIT 1",
            (unit.owner_id, start)).fetchone()
        if row is None:
            return False
    return True


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
        currency: str, ends_at: str | None, moment: datetime, now: str,
        rate_table: str, rate_owner_column: str, charge_table: str,
        charge_owner_column: str, credential_uuid: str | None,
        attempted: set[tuple[str, str]],
        charge_subscription_id: int | None = None,
        current_only: bool = False) -> int:
    """Materialize one recurring stream using the shared Plan algorithm."""
    ended = None
    if ends_at:
        ended = parse_runtime_timestamp(ends_at)
        if ended is None:
            raise ValueError("billing timestamp cannot be empty")
        ended = ended.replace(microsecond=0)
    cutoff = min(moment, ended) if ended else moment
    anchor_day = anchor.day
    start_dt, end_dt = (_period(_as_utc_datetime(moment), anchor_day)
                        if current_only else
                        _period(_as_utc_datetime(anchor), anchor_day))
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
                conn.execute(
                    "UPDATE billing_period_charges SET "
                    "account_identity_id=(SELECT account_id FROM billing_contracts WHERE id=?),"
                    "contract_uuid_snapshot=(SELECT uuid FROM billing_contracts WHERE id=?),"
                    "billing_unit_id=CASE WHEN credential_uuid IS NOT NULL THEN credential_uuid "
                    "ELSE 'contract:' || (SELECT uuid FROM billing_contracts WHERE id=?) END "
                    "WHERE id=last_insert_rowid()", (owner_id, owner_id, owner_id))
            else:
                conn.execute(
                    f"INSERT INTO {charge_table}"
                    f"({charge_owner_column},subscription_id,period_start,period_end,recurring_charge,currency,"
                    "normalized_recurring_cost,base_currency,fx_rate_date,finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (owner_id, charge_subscription_id, start, end, *values,
                     now if can_finalize else None),
                )
                conn.execute(
                    "UPDATE agent_subscription_period_charges SET "
                    "subscription_uuid_snapshot=(SELECT uuid FROM agent_subscriptions WHERE id=?),"
                    "instance_uuid_snapshot=(SELECT uuid FROM agent_subscription_instances WHERE id=?),"
                    "subscription_name_snapshot=(SELECT name FROM agent_subscriptions WHERE id=?),"
                    "instance_label_snapshot=(SELECT label FROM agent_subscription_instances WHERE id=?) "
                    "WHERE id=last_insert_rowid()",
                    (charge_subscription_id, owner_id, charge_subscription_id, owner_id),
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
                            now: str, period_starts: set[str]) -> int:
    """Freeze only current periods touched by this materialization.

    A normal worker must not turn an old, previously missed row into a new
    financial/export fact.  Historical repair is intentionally outside the
    runtime path, so finalization is bounded by the current period starts
    resolved from the live billing units.
    """
    if not period_starts:
        return 0
    marks = ",".join("?" for _ in period_starts)
    finalized = conn.execute(
        f"UPDATE {table} SET finalized_at=? WHERE finalized_at IS NULL "
        "AND normalized_recurring_cost IS NOT NULL "
        "AND (currency='CNY' OR fx_rate_date IS NOT NULL) "
        f"AND period_start IN ({marks})",
        (now, *sorted(period_starts)),
    )
    return max(finalized.rowcount, 0)


def materialize_period_charges_conn(conn: sqlite3.Connection,
                                    at: datetime | None = None,
                                    *, current_only: bool = True) -> int:
    """Materialize proxy charges on a caller-owned transaction."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    now = format_utc(moment)
    changed = 0
    attempted: set[tuple[str, str]] = set()
    period_starts: set[str] = set()
    for unit in BillingUnitResolver.proxy_units(conn, at=moment):
        period_starts.add(_current_period_start(moment, unit.anchor_day))
        changed += _materialize_period_stream(
            conn, owner_id=unit.owner_id, anchor=unit.valid_from,
            currency=unit.currency,
            ends_at=BillingUnitResolver.end_stamp(unit), moment=moment,
            now=now, rate_table="billing_rate_events",
            rate_owner_column="contract_id",
            charge_table="billing_period_charges",
            charge_owner_column="contract_id",
            credential_uuid=unit.credential_uuid, attempted=attempted,
            current_only=current_only)
    changed += _finalize_period_stream(
        conn, "billing_period_charges", now, period_starts)
    append_billing_export_events_for_finalized_at(conn, now)
    return changed


def materialize_period_charges(db_path: str,
                               at: datetime | None = None) -> int:
    """Create/update current charges idempotently and freeze closed periods."""
    conn = sqlite_runtime.connect(db_path, "billing_write")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            return materialize_period_charges_conn(conn, at, current_only=True)
    finally:
        conn.close()


def materialize_agent_subscription_charges_conn(
        conn: sqlite3.Connection, at: datetime | None = None,
        *, current_only: bool = True) -> int:
    """Materialize Agent charges on a caller-owned transaction."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    now = format_utc(moment)
    changed = 0
    attempted: set[tuple[str, str]] = set()
    period_starts: set[str] = set()
    for unit in BillingUnitResolver.agent_units(conn, at=moment):
        period_starts.add(_current_period_start(moment, unit.anchor_day))
        changed += _materialize_period_stream(
            conn, owner_id=unit.owner_id, anchor=unit.valid_from,
            currency=unit.currency, ends_at=BillingUnitResolver.end_stamp(unit),
            moment=moment, now=now,
            rate_table="agent_subscription_rate_events",
            rate_owner_column="instance_id",
            charge_table="agent_subscription_period_charges",
            charge_owner_column="instance_id", credential_uuid=None,
            charge_subscription_id=unit.subscription_id,
            attempted=attempted, current_only=current_only)
        changed += _materialize_agent_charge_allocations(
            conn, unit.owner_id, unit.account_id, unit.valid_from,
            unit.subscription_id, now, moment=moment)
    changed += _finalize_period_stream(
        conn, "agent_subscription_period_charges", now, period_starts)
    append_billing_export_events_for_finalized_at(conn, now)
    return changed


def materialize_agent_subscription_charges(
        db_path: str, at: datetime | None = None) -> int:
    """Materialize agent subscription instances with Plan's exact rules."""
    conn = sqlite_runtime.connect(db_path, "billing_write")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            return materialize_agent_subscription_charges_conn(conn, at, current_only=True)
    finally:
        conn.close()


def materialize_all_period_charges(db_path: str,
                                   at: datetime | None = None) -> int:
    """Materialize proxy Plans and agent subscription instances."""
    return (materialize_period_charges(db_path, at)
            + materialize_agent_subscription_charges(db_path, at))


def _materialize_agent_charge_allocations(
        conn: sqlite3.Connection, instance_id: int, account_id: int | None,
        valid_from: date, subscription_id: int | None, now: str,
        *, moment: datetime | None = None) -> int:
    """Snapshot active software bindings for finalized instance charges."""
    if subscription_id is None:
        return 0
    current_start = _current_period_start(moment or utc_now(),
                                          valid_from.day)
    charge_sql = (
        "SELECT id,period_start,recurring_charge,normalized_recurring_cost,"
        "currency,base_currency,fx_rate_date,finalized_at "
        "FROM agent_subscription_period_charges "
        "WHERE instance_id=? AND finalized_at IS NOT NULL "
        "AND period_start=?")
    charge_params: tuple[object, ...] = (instance_id, current_start)
    charges = conn.execute(charge_sql, charge_params).fetchall()
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
            "WHERE subscription_id=? AND valid_from<=? "
            "AND (ends_at IS NULL OR ends_at>?) "
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
