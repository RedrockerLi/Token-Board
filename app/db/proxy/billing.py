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

Which period is "current" is decided by one resolver
(:func:`resolve_unit_period`): the recorded ledger wins while it still covers
the moment, otherwise the unit rolls from the previous period's end — driven
by a fresh observed monthly reset (:mod:`app.services.key_usage`) when the
upstream reports one, and by the anchor-day calendar otherwise.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime

from app.core import sqlite_runtime
from app.core.time import (
    as_utc, billing_period, format_utc, next_billing_period,
    parse_runtime_timestamp, utc_now,
)
from app.db.proxy.billing_export import ensure_billing_export_events_conn
from app.services import fx
from app.services.billing_units import BillingUnit, BillingUnitResolver


def _period(moment: datetime, anchor: int) -> tuple[datetime, datetime]:
    period = billing_period(moment, anchor)
    return period.start, period.end


def _as_utc_datetime(value: datetime | date) -> datetime:
    """Normalize a calendar date to a UTC datetime for period arithmetic."""
    return as_utc(value).replace(microsecond=0)


def _next_period(start: datetime, anchor: int) -> tuple[datetime, datetime]:
    period = next_billing_period(start, anchor)
    return period.start, period.end


def _period_chain(start_dt: datetime, end_dt: datetime,
                  anchor: int) -> Iterator[tuple[datetime, datetime]]:
    """Yield the anchor periods of one stream, starting with the first."""
    while True:
        yield start_dt, end_dt
        start_dt, end_dt = _next_period(start_dt, anchor)


def _current_period_start(moment: datetime, anchor: int) -> str:
    return format_utc(billing_period(moment, anchor).start)


def _timestamp(value: object) -> datetime | None:
    """Parse one optional runtime timestamp, ignoring malformed values."""
    try:
        return parse_runtime_timestamp(value)
    except ValueError:
        return None


def resolve_unit_period(conn: sqlite3.Connection, unit: BillingUnit,
                        moment: datetime) -> tuple[datetime, datetime] | None:
    """Resolve the billable period of one proxy unit at *moment*.

    The ledger is authoritative: while a recorded charge covers *moment* its
    period is returned unchanged.  Otherwise the unit rolls forward from the
    previous period's end, driven by the observed monthly reset when the
    upstream usage snapshot is fresh and by the anchor day otherwise.  A unit
    whose billing start lies in the future has no period yet.
    """
    moment = as_utc(moment).replace(microsecond=0)
    if moment < as_utc(unit.valid_from):
        return None
    row = conn.execute(
        "SELECT period_start,period_end FROM billing_period_charges "
        "WHERE contract_id=? AND credential_uuid IS ? AND period_start<=? "
        "ORDER BY period_start DESC LIMIT 1",
        (unit.contract_id, unit.credential_uuid, format_utc(moment)),
    ).fetchone()
    previous_end = _timestamp(row["period_end"]) if row is not None else None
    if previous_end is not None and moment < previous_end:
        previous_start = _timestamp(row["period_start"])
        if previous_start is not None:
            return previous_start, previous_end
    anchor = billing_period(moment, unit.anchor_day)
    observed = unit.observed_period
    if observed is None or not unit.observed_fresh(moment):
        # The anchor calendar: a rolling start lands exactly on the previous
        # period's end, otherwise on the boundary containing the moment.
        return previous_end or anchor.start, anchor.end
    start = previous_end or observed.start
    # An upstream window can begin before this machine knew the subscription;
    # the valid_from floor must hold for the first observed period too, or the
    # row would start before the paid term and find no price event.
    valid_from = as_utc(unit.valid_from)
    if start < valid_from:
        start = valid_from
    end = observed.end
    if end <= start:
        end = billing_period(start, unit.anchor_day).end
    return start, end


def proxy_billing_ready(conn: sqlite3.Connection, account_id: int,
                        at: datetime | None = None) -> bool:
    """Whether every currently billable proxy unit has a charge row."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    units = [u for u in BillingUnitResolver.proxy_units(conn, at=moment)
             if u.account_id == account_id]
    for unit in units:
        period = resolve_unit_period(conn, unit, moment)
        if period is None:
            continue
        row = conn.execute(
            "SELECT 1 FROM billing_period_charges WHERE contract_id=? "
            "AND credential_uuid IS ? AND period_start=? LIMIT 1",
            (unit.contract_id, unit.credential_uuid,
             format_utc(period[0]))).fetchone()
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
                       currency: str, start_date: str
                       ) -> tuple[float | None, str | None, bool]:
    """Convert ``price`` to CNY at the rate locked on the period start date.

    A USD row is locked using the exact period-start rate when available. FX
    prewarming happens before the caller's write transaction; this function is
    deliberately read-only so it can never perform network I/O while SQLite
    writes are locked. A currency with no stored rate remains pending.
    """
    if currency == "CNY":
        return price, None, True
    resolution = fx.FxRateResolver.resolve(conn, currency, "CNY", start_date)
    if resolution.source_date is None:
        return None, None, False
    return price * resolution.rate, resolution.source_date, True


def prewarm_period_fx_rates_conn(conn: sqlite3.Connection,
                                 at: datetime | None = None,
                                 *, agent: bool = False) -> None:
    """Fetch missing period-start FX rows before a write transaction begins.

    The caller owns the connection, but this helper must be called while it is
    outside a transaction. The materializer itself only resolves stored rows.
    """
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    units = (BillingUnitResolver.agent_units(conn, at=moment)
             if agent else BillingUnitResolver.proxy_units(conn, at=moment))
    seen: set[tuple[str, str]] = set()
    for unit in units:
        if unit.currency == "CNY":
            continue
        if agent:
            start_stamp = _current_period_start(moment, unit.anchor_day)
        else:
            period = resolve_unit_period(conn, unit, moment)
            if period is None:
                continue
            start_stamp = format_utc(period[0])
        target = start_stamp[:10]
        if agent:
            frozen = conn.execute(
                "SELECT 1 FROM agent_subscription_period_charges "
                "WHERE instance_id=? AND period_start=? AND is_finalized=1 LIMIT 1",
                (unit.owner_id, start_stamp),
            ).fetchone()
        else:
            frozen = conn.execute(
                "SELECT 1 FROM billing_period_charges "
                "WHERE contract_id=? AND credential_uuid IS ? AND period_start=? "
                "AND finalized_at IS NOT NULL LIMIT 1",
                (unit.contract_id, unit.credential_uuid, start_stamp),
            ).fetchone()
        if frozen is not None:
            # A failed historical fetch may have frozen a nearest stored rate
            # without creating an exact FX row. Never retry the network for a
            # charge that is already a financial fact.
            continue
        key = (unit.currency, target)
        if key in seen:
            continue
        seen.add(key)
        fx.FxRateResolver.ensure(conn, unit.currency, "CNY", target)


def _rate_from_table(conn: sqlite3.Connection, table: str, owner_column: str,
                     owner_id: int, start: str, as_of: str,
                     effective_column: str = "effective_at") -> float:
    """Read the rate that was effective when a period started."""
    row = conn.execute(
        f"SELECT recurring_price FROM {table} "
        f"WHERE {owner_column}=? AND {effective_column}<=? "
        f"ORDER BY {effective_column} DESC,id DESC LIMIT 1",
        (owner_id, start),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _materialize_period_stream(
        conn: sqlite3.Connection, *, owner_id: int, anchor: date,
        currency: str, ends_at: str | None, ends_on: date | None,
        moment: datetime, now: str,
        rate_table: str, rate_owner_column: str, charge_table: str,
        charge_owner_column: str, credential_uuid: str | None,
        charge_subscription_id: int | None = None,
        charge_software_id: int | None = None,
        current_only: bool = False,
        period: tuple[datetime, datetime] | None = None) -> int:
    """Materialize one recurring stream using the shared Plan algorithm.

    ``period`` overrides the anchoring rules with one already resolved
    ``(start, end)`` period (the reset-driven path); it is materialized alone,
    because an observed period does not advance on the anchor calendar.
    """
    ended = None
    if ends_at:
        ended = parse_runtime_timestamp(ends_at)
        if ended is None:
            raise ValueError("billing timestamp cannot be empty")
        ended = ended.replace(microsecond=0)
    cutoff = min(moment, ended) if ended else moment
    anchor_day = anchor.day
    if period is not None:
        periods = (period,)
    else:
        first = (_period(_as_utc_datetime(moment), anchor_day) if current_only else
                 _period(_as_utc_datetime(anchor), anchor_day))
        periods = _period_chain(*first, anchor_day)
    changed = 0
    for start_dt, end_dt in periods:
        if start_dt > cutoff:
            break
        if ends_on is not None and start_dt.date() >= ends_on:
            break
        start, end = format_utc(start_dt), format_utc(end_dt)
        has_credential = charge_table == "billing_period_charges"
        agent_charge = charge_table == "agent_subscription_period_charges"
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
                f"SELECT id,software_id,is_finalized,finalized_on,recurring_charge,currency,"
                f"normalized_recurring_cost,fx_rate_date FROM {charge_table} "
                f"WHERE {charge_owner_column}=? AND period_start=?",
                (owner_id, start),
            ).fetchone()
        # A finalized row is a financial fact. Do not even resolve its rate
        # again: this avoids network access for permanently-fallback FX rows
        # and guarantees that later price/rate events cannot rewrite history.
        already_finalized = (existing is not None and (
            existing["finalized_at"] is not None if has_credential else
            bool(existing["is_finalized"])))
        if already_finalized:
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
                    conn, price, row_currency, start[:10])
            currency = row_currency
        else:
            price = _rate_from_table(
                conn, rate_table, rate_owner_column, owner_id, start, start,
                "effective_on" if agent_charge else "effective_at")
            normalized, fx_date, can_finalize = _normalized_charge(
                conn, price, currency, start[:10])
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
                    f"({charge_owner_column},subscription_id,software_id,period_start,period_end,recurring_charge,currency,"
                    "normalized_recurring_cost,base_currency,fx_rate_date,is_finalized,finalized_on) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (owner_id, charge_subscription_id, charge_software_id,
                      start, end, *values,
                     1 if can_finalize else 0,
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
        elif (existing["finalized_at"] is None if has_credential
              else not existing["is_finalized"]):
            if can_finalize:
                if has_credential:
                    conn.execute(
                        f"UPDATE {charge_table} SET normalized_recurring_cost=?,"
                        "base_currency=?,fx_rate_date=?,finalized_at=? WHERE id=?",
                        (normalized, "CNY", fx_date, now, existing["id"]),
                    )
                else:
                    conn.execute(
                        f"UPDATE {charge_table} SET normalized_recurring_cost=?,"
                        "base_currency=?,fx_rate_date=?,is_finalized=1,"
                        "finalized_on=? WHERE id=?",
                        (normalized, "CNY", fx_date, now, existing["id"]),
                    )
                changed += 1
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
    if table == "agent_subscription_period_charges":
        finalized = conn.execute(
            f"UPDATE {table} SET is_finalized=1,finalized_on=? "
            "WHERE is_finalized=0 AND normalized_recurring_cost IS NOT NULL "
            "AND (currency='CNY' OR fx_rate_date IS NOT NULL) "
            f"AND period_start IN ({marks})",
            (now, *sorted(period_starts)),
        )
    else:
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
    period_starts: set[str] = set()
    for unit in BillingUnitResolver.proxy_units(conn, at=moment):
        period = resolve_unit_period(conn, unit, moment)
        if period is None:
            continue
        if current_only:
            period_starts.add(format_utc(period[0]))
        else:
            period_starts.add(_current_period_start(moment, unit.anchor_day))
        changed += _materialize_period_stream(
            conn, owner_id=unit.owner_id, anchor=unit.valid_from,
            currency=unit.currency,
            ends_at=BillingUnitResolver.end_stamp(unit), ends_on=None,
            moment=moment,
            now=now, rate_table="billing_rate_events",
            rate_owner_column="contract_id",
            charge_table="billing_period_charges",
            charge_owner_column="contract_id",
            credential_uuid=unit.credential_uuid,
            current_only=current_only,
            period=period if current_only else None)
    changed += _finalize_period_stream(
        conn, "billing_period_charges", now, period_starts)
    changed += ensure_billing_export_events_conn(conn)
    return changed


def materialize_period_charges(db_path: str,
                               at: datetime | None = None) -> int:
    """Create/update current charges idempotently and freeze closed periods."""
    conn = sqlite_runtime.connect(db_path, "billing_write")
    try:
        prewarm_period_fx_rates_conn(conn, at, agent=False)
        with sqlite_runtime.transaction(conn, "immediate"):
            return materialize_period_charges_conn(conn, at, current_only=True)
    finally:
        conn.close()


def materialize_agent_subscription_charges_conn(
        conn: sqlite3.Connection, at: datetime | None = None,
        *, current_only: bool = True,
        include_ended: bool = False) -> int:
    """Materialize directly-owned Agent charges on a caller-owned transaction."""
    moment = as_utc(at or utc_now()).replace(microsecond=0)
    now = moment.date().isoformat()
    changed = 0
    period_starts: set[str] = set()
    for unit in BillingUnitResolver.agent_units(
            conn, at=moment, include_ended=include_ended):
        current_start = _current_period_start(moment, unit.anchor_day)
        period_starts.add(current_start)
        changed += _attach_agent_charge_owner(
            conn, unit.owner_id, unit.account_id, current_start)
        changed += _materialize_period_stream(
            conn, owner_id=unit.owner_id, anchor=unit.valid_from,
            currency=unit.currency, ends_at=None, ends_on=unit.ends_on,
            moment=moment, now=now,
            rate_table="agent_subscription_rate_events",
            rate_owner_column="instance_id",
            charge_table="agent_subscription_period_charges",
            charge_owner_column="instance_id", credential_uuid=None,
            charge_subscription_id=unit.subscription_id,
            charge_software_id=unit.account_id,
            current_only=current_only)
    changed += _finalize_period_stream(
        conn, "agent_subscription_period_charges", now, period_starts)
    changed += ensure_billing_export_events_conn(conn)
    return changed


def materialize_agent_subscription_charges(
        db_path: str, at: datetime | None = None) -> int:
    """Materialize agent subscription instances with Plan's exact rules."""
    conn = sqlite_runtime.connect(db_path, "billing_write")
    try:
        prewarm_period_fx_rates_conn(conn, at, agent=True)
        with sqlite_runtime.transaction(conn, "immediate"):
            return materialize_agent_subscription_charges_conn(conn, at, current_only=True)
    finally:
        conn.close()


def materialize_all_period_charges(db_path: str,
                                   at: datetime | None = None) -> int:
    """Materialize proxy Plans and agent subscription instances."""
    return (materialize_period_charges(db_path, at)
            + materialize_agent_subscription_charges(db_path, at))


def _attach_agent_charge_owner(conn: sqlite3.Connection, instance_id: int,
                               software_id: int | None,
                               period_start: str) -> int:
    """Attach the current binding to an existing unowned current charge.

    V2.1 could create a finalized period charge before its allocation. Once a
    billing run sees an effective binding, that charge can be assigned
    directly unless a legacy allocation already owns it; the latter remains
    historical and is intentionally left untouched.
    """
    if software_id is None:
        return 0
    cursor = conn.execute(
        "UPDATE agent_subscription_period_charges SET software_id=? "
        "WHERE instance_id=? AND period_start=? AND software_id IS NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM agent_subscription_charge_allocations a "
        "  WHERE a.period_charge_id=agent_subscription_period_charges.id"
        ")",
        (software_id, instance_id, period_start),
    )
    return max(cursor.rowcount, 0)
