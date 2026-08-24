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


def _nearest_rate(conn: sqlite3.Connection, currency: str,
                  start_date: str) -> tuple[float, str | None]:
    """Nearest stored rate for a period start, read-only (never fetches).

    The exact row for *start_date* wins; otherwise the most recent earlier
    row; when *start_date* precedes every stored row, the earliest stored row
    (so pre-1999 or never-fetched periods are not silently undervalued); a
    currency pair that has never been stored degrades to 1.0 (no conversion).
    Returns (rate, fx_date).
    """
    row = conn.execute(
        "SELECT rate,date FROM fx_rates WHERE base_currency=? "
        "AND quote_currency='CNY' AND date<=date(?) "
        "ORDER BY date DESC LIMIT 1", (currency, start_date),
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
    exact = conn.execute(
        "SELECT rate,date FROM fx_rates WHERE base_currency=? "
        "AND quote_currency='CNY' AND date=?",
        (currency, start_date)).fetchone()
    if exact is not None:
        return price * float(exact[0]), str(exact[1])  # locked, zero HTTP
    key = (currency, start_date)
    if attempted is None or key not in attempted:
        fx.ensure_rate(conn, currency, "CNY", date=start_date)
        if attempted is not None:
            attempted.add(key)
    exact = conn.execute(
        "SELECT rate,date FROM fx_rates WHERE base_currency=? "
        "AND quote_currency='CNY' AND date=?",
        (currency, start_date)).fetchone()
    if exact is not None:
        return price * float(exact[0]), str(exact[1])  # locked by this fetch
    rate, fx_date = _nearest_rate(conn, currency, start_date)  # provisional
    return price * rate, fx_date


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
    ended = _parse_stamp(valid_until) if valid_until else None
    cutoff = min(moment, ended) if ended else moment
    anchor_day = anchor.day
    start_dt, end_dt = _period(_as_utc_datetime(anchor), anchor_day)
    changed = 0
    while start_dt <= cutoff:
        start, end = _stamp(start_dt), _stamp(end_dt)
        as_of = _stamp(min(moment, end_dt))
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
    moment = _utc(at)
    now = _stamp(moment)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        attempted: set[tuple[str, str]] = set()
        contracts = conn.execute(
            "SELECT bc.id,bc.account_id,bc.billing_scope,bc.currency,"
            "bc.valid_from,bc.valid_until,a.valid_from account_valid_from,"
            "a.created_at account_created_at "
            "FROM billing_contracts bc JOIN accounts a ON a.id=bc.account_id "
            "WHERE bc.charge_type='recurring' AND bc.valid_from<=? "
            "AND a.account_kind='proxy'",
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
                changed += _materialize_period_stream(
                    conn, owner_id=contract["id"], anchor=unit["anchor"],
                    currency=contract["currency"],
                    valid_until=contract["valid_until"], moment=moment, now=now,
                    rate_table="billing_rate_events",
                    rate_owner_column="contract_id",
                    charge_table="billing_period_charges",
                    charge_owner_column="contract_id",
                    credential_uuid=unit["credential_uuid"], attempted=attempted)
        # Only locked rows may be frozen: a USD row whose rate is still a
        # provisional (unlocked) value must keep retrying the historical
        # fetch instead of being finalized on a degraded rate.
        changed += _finalize_period_stream(conn, "billing_period_charges", now)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def materialize_agent_subscription_charges(
        db_path: str, at: datetime | None = None) -> int:
    """Materialize agent subscription instances with Plan's exact rules."""
    moment = _utc(at)
    now = _stamp(moment)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        attempted: set[tuple[str, str]] = set()
        instances = conn.execute(
            "SELECT i.id,i.subscription_id,i.valid_from,i.valid_until,s.currency "
            "FROM agent_subscription_instances i "
            "JOIN agent_subscriptions s ON s.id=i.subscription_id "
            "WHERE (s.lifecycle_state='active' OR "
            "(s.lifecycle_state='deleted' AND s.valid_until>=?)) "
            "AND (i.lifecycle_state='active' OR "
            "(i.lifecycle_state='deleted' AND i.valid_until>=?)) "
            "AND i.valid_from<=?",
            (now, now, now),
        ).fetchall()
        for instance in instances:
            anchor = _parse_iso_date(instance["valid_from"][:10])
            changed += _materialize_period_stream(
                conn, owner_id=instance["id"], anchor=anchor,
                currency=instance["currency"], valid_until=instance["valid_until"],
                moment=moment, now=now,
                rate_table="agent_subscription_rate_events",
                rate_owner_column="instance_id",
                charge_table="agent_subscription_period_charges",
                charge_owner_column="instance_id", credential_uuid=None,
                charge_subscription_id=instance["subscription_id"],
                attempted=attempted)
        changed += _finalize_period_stream(
            conn, "agent_subscription_period_charges", now)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def materialize_all_period_charges(db_path: str,
                                   at: datetime | None = None) -> int:
    """Materialize proxy Plans and agent subscription instances."""
    return (materialize_period_charges(db_path, at)
            + materialize_agent_subscription_charges(db_path, at))
