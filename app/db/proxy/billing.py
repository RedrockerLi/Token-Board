"""Recurring-charge ledger materialization for the normalized V1 schema."""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta, timezone

from app.db.proxy.common import _parse_iso_date, _parse_utc_timestamp
from app.services import fx


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


def _as_utc_datetime(value: datetime | date) -> datetime:
    """Normalize a calendar date to a UTC datetime for period arithmetic."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


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


def _fx_for_month(conn: sqlite3.Connection, currency: str, month: str,
                  today: str) -> tuple[float, str | None]:
    """Nearest stored rate for a billing month, with earliest-row fallback.

    Mirrors ``app.services.fx.rate_for_month``: the current month prefers
    today's rate, past months freeze at the rate on the month's first day,
    dates before the first stored row use the earliest stored rate, and a
    currency pair that has never been stored degrades to 1.0 (no conversion).
    """
    boundary = today if month >= today[:7] else month + "-01"
    row = conn.execute(
        "SELECT rate,date FROM fx_rates WHERE base_currency=? "
        "AND quote_currency='CNY' AND date<=date(?) "
        "ORDER BY date DESC LIMIT 1", (currency, boundary),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT rate,date FROM fx_rates WHERE base_currency=? "
            "AND quote_currency='CNY' ORDER BY date ASC LIMIT 1", (currency,),
        ).fetchone()
    if row is None:
        return 1.0, None
    return float(row[0]), str(row[1])


def _normalized_charge(conn: sqlite3.Connection, price: float,
                       currency: str, month: str, today: str
                       ) -> tuple[float, str | None]:
    if currency == "CNY":
        return price, None
    if month >= today[:7]:
        # Current month refreshes with today's rate (fetch-on-demand, best
        # effort; failures fall back to stored rates inside fx.ensure_rate).
        fx.ensure_rate(conn, currency, "CNY", date=today)
    rate, fx_date = _fx_for_month(conn, currency, month, today)
    return price * rate, fx_date


def materialize_period_charges(db_path: str,
                               at: datetime | None = None) -> int:
    """Create/update current charges idempotently and freeze closed periods."""
    moment = _utc(at)
    now = _stamp(moment)
    today = now[:10]
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        contracts = conn.execute(
            "SELECT bc.id,bc.account_id,bc.billing_scope,bc.currency,"
            "bc.valid_from,bc.valid_until,a.valid_from account_valid_from,"
            "a.created_at account_created_at "
            "FROM billing_contracts bc JOIN accounts a ON a.id=bc.account_id "
            "WHERE bc.charge_type='recurring' AND bc.valid_from<=?",
            (now,),
        ).fetchall()
        for contract in contracts:
            valid_until = (_parse_stamp(contract["valid_until"])
                           if contract["valid_until"] else None)
            cutoff = min(moment, valid_until) if valid_until else moment
            # Per-unit lifecycles: each plan key (or agent account) anchors
            # its own administrative month on valid_from/created_at.  The
            # contract's valid_from is only an eligibility lower bound, never
            # the period origin (contracts migrated from V0 default to 1970).
            units: list[dict] = []
            if contract["billing_scope"] == "credential":
                seen_masks: set[tuple[int, str]] = set()
                for row in conn.execute(
                    "SELECT c.uuid,c.key_masked,c.valid_from,c.created_at "
                    "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id "
                    "WHERE u.account_id=? "
                    "AND EXISTS(SELECT 1 FROM upstream_secrets s "
                    "WHERE s.credential_uuid=c.uuid) "
                    "AND (c.disabled_at IS NULL OR c.disabled_at>?) "
                    "AND (c.deleted_at IS NULL OR c.deleted_at>?) "
                    "ORDER BY c.position,c.runtime_id",
                    (contract["account_id"], now, now),
                ):
                    identity = (contract["account_id"], row["key_masked"])
                    if identity in seen_masks:
                        continue  # local+cloud duplicate: bill the local slot once
                    seen_masks.add(identity)
                    anchor = (_parse_iso_date(row["valid_from"])
                              or _parse_utc_timestamp(row["created_at"]).date())
                    units.append({"credential_uuid": row["uuid"], "anchor": anchor})
            else:
                anchor = (_parse_iso_date(contract["account_valid_from"])
                          or _parse_utc_timestamp(
                              contract["account_created_at"]).date())
                units.append({"credential_uuid": None, "anchor": anchor})
            for unit in units:
                anchor_day = unit["anchor"].day
                start_dt, end_dt = _period(
                    _as_utc_datetime(unit["anchor"]), anchor_day)
                # Include the current period immediately when its anchor is
                # the unit's start.  A strict '<' would leave a newly created
                # key without a charge until the next month.
                while start_dt <= cutoff:
                    start, end = _stamp(start_dt), _stamp(end_dt)
                    as_of = _stamp(min(moment, end_dt))
                    price = _rate(conn, contract["id"], start, as_of)
                    normalized, fx_date = _normalized_charge(
                        conn, price, contract["currency"], start[:7], today)
                    existing = conn.execute(
                        "SELECT id,finalized_at FROM billing_period_charges "
                        "WHERE contract_id=? AND credential_uuid IS ? AND period_start=?",
                        (contract["id"], unit["credential_uuid"], start),
                    ).fetchone()
                    values = (price, contract["currency"], normalized,
                              "CNY", fx_date)
                    if existing is None:
                        conn.execute(
                            "INSERT INTO billing_period_charges"
                            "(contract_id,credential_uuid,period_start,period_end,"
                            "recurring_charge,currency,normalized_recurring_cost,"
                            "base_currency,fx_rate_date) VALUES(?,?,?,?,?,?,?,?,?)",
                            (contract["id"], unit["credential_uuid"], start, end,
                             *values),
                        )
                        changed += 1
                    elif existing["finalized_at"] is None:
                        conn.execute(
                            "UPDATE billing_period_charges SET recurring_charge=?,"
                            "currency=?,normalized_recurring_cost=?,base_currency=?,"
                            "fx_rate_date=? WHERE id=?",
                            (*values, existing["id"]),
                        )
                    start_dt, end_dt = _next_period(start_dt, anchor_day)
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
