"""Recurring-charge ledger materialization for the normalized V1 schema."""

from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timedelta, timezone


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _period(moment: datetime, anchor: int) -> tuple[datetime, datetime]:
    day = min(anchor, calendar.monthrange(moment.year, moment.month)[1])
    start = datetime(moment.year, moment.month, day, tzinfo=timezone.utc)
    if moment < start:
        year, month = (moment.year - 1, 12) if moment.month == 1 else (moment.year, moment.month - 1)
        day = min(anchor, calendar.monthrange(year, month)[1])
        start = datetime(year, month, day, tzinfo=timezone.utc)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = datetime(year, month, min(anchor, calendar.monthrange(year, month)[1]),
                   tzinfo=timezone.utc)
    return start, end


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _utc(parsed)


def _next_period(start: datetime, anchor: int) -> tuple[datetime, datetime]:
    probe = start + timedelta(days=32)
    return _period(probe, anchor)


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
                       currency: str, period_start: str
                       ) -> tuple[float | None, str | None]:
    if currency == "CNY":
        return price, None
    row = conn.execute(
        "SELECT rate,date FROM fx_rates WHERE base_currency=? "
        "AND quote_currency='CNY' AND date<=date(?) "
        "ORDER BY date DESC LIMIT 1",
        (currency, period_start),
    ).fetchone()
    if row is None:
        return None, None
    return price * float(row[0]), str(row[1])


def materialize_period_charges(db_path: str,
                               at: datetime | None = None) -> int:
    """Create/update current charges idempotently and freeze closed periods."""
    moment = _utc(at)
    now = _stamp(moment)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        contracts = conn.execute(
            "SELECT id,account_id,billing_scope,currency,billing_anchor_day,"
            "valid_from,valid_until "
            "FROM billing_contracts WHERE charge_type='recurring' "
            "AND valid_from<=?",
            (now,),
        ).fetchall()
        for contract in contracts:
            valid_from = _parse_stamp(contract["valid_from"])
            valid_until = (_parse_stamp(contract["valid_until"])
                           if contract["valid_until"] else None)
            cutoff = min(moment, valid_until) if valid_until else moment
            start_dt, end_dt = _period(valid_from, contract["billing_anchor_day"])
            # Include the current period immediately when its anchor is the
            # contract's valid_from.  A strict '<' leaves a newly created
            # contract without a charge until the next month.
            while start_dt <= cutoff:
                start, end = _stamp(start_dt), _stamp(end_dt)
                as_of = _stamp(min(moment, end_dt))
                price = _rate(conn, contract["id"], start, as_of)
                normalized, fx_date = _normalized_charge(
                    conn, price, contract["currency"], start)
                units: list[str | None]
                if contract["billing_scope"] == "credential":
                    units = [row[0] for row in conn.execute(
                        "SELECT c.uuid FROM upstream_credentials c JOIN upstreams u "
                        "ON u.id=c.upstream_id WHERE u.account_id=? "
                        "AND COALESCE(c.valid_from,c.created_at)<? "
                        "AND (c.deleted_at IS NULL OR c.deleted_at>?)",
                        (contract["account_id"], end, start),
                    )]
                else:
                    units = [None]
                for credential_uuid in units:
                    existing = conn.execute(
                        "SELECT id,finalized_at FROM billing_period_charges "
                        "WHERE contract_id=? AND credential_uuid IS ? AND period_start=?",
                        (contract["id"], credential_uuid, start),
                    ).fetchone()
                    values = (price, contract["currency"], normalized,
                              "CNY", fx_date)
                    if existing is None:
                        conn.execute(
                            "INSERT INTO billing_period_charges"
                            "(contract_id,credential_uuid,period_start,period_end,"
                            "recurring_charge,currency,normalized_recurring_cost,"
                            "base_currency,fx_rate_date) VALUES(?,?,?,?,?,?,?,?,?)",
                            (contract["id"], credential_uuid, start, end, *values),
                        )
                        changed += 1
                    elif existing["finalized_at"] is None:
                        conn.execute(
                            "UPDATE billing_period_charges SET recurring_charge=?,"
                            "currency=?,normalized_recurring_cost=?,base_currency=?,"
                            "fx_rate_date=? WHERE id=?",
                            (*values, existing["id"]),
                        )
                start_dt, end_dt = _next_period(
                    start_dt, contract["billing_anchor_day"])
        finalized = conn.execute(
            "UPDATE billing_period_charges SET finalized_at=? "
            "WHERE finalized_at IS NULL AND normalized_recurring_cost IS NOT NULL "
            "AND period_end<=?", (now, now)
        )
        changed += max(finalized.rowcount, 0)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
