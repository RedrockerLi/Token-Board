"""Python-side SQLite access layer for the proxy tables.

Used by the Flask dashboard to manage upstream accounts, local API keys,
model pricing, and to read billing/usage data written by the C++ proxy.

Thread-safe: each method opens its own connection (SQLite in WAL mode
supports concurrent readers alongside a single writer).
"""

import json
import os
import secrets
import sqlite3
import string
import urllib.request
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from app.domain.account_types import (
    ACCOUNT_TYPES,
    deletion_policy,
    holds_keys as type_holds_keys,
    import_types,
    is_routable,
    is_subscription,
    spec,
    subscription_types,
)


def _generate_key() -> str:
    """Generate a local proxy key: 'tb-' + 32 random hex chars."""
    return "tb-" + secrets.token_hex(16)


def mask_key(key: str) -> str:
    """Mask an upstream key for display: 'sk-abc…' (first 6 + '…' + last 4)."""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "…"
    return f"{key[:6]}…{key[-4:]}"


UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_iso_date(value: object) -> date | None:
    """Parse a user-facing UTC calendar date, rejecting ambiguous values."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("订阅起始日必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("订阅起始日必须是 YYYY-MM-DD") from exc


def _parse_utc_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # SQLite's datetime('now') has a space separator and no offset.
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _period_start(month: str, anchor_day: int) -> datetime:
    year, month_number = (int(part) for part in month.split("-", 1))
    return datetime(year, month_number, min(anchor_day, monthrange(year, month_number)[1]), tzinfo=UTC)


def _previous_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-", 1))
    return f"{year - 1:04d}-12" if number == 1 else f"{year:04d}-{number - 1:02d}"


def _next_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-", 1))
    return f"{year + 1:04d}-01" if number == 12 else f"{year:04d}-{number + 1:02d}"


def _billing_period_month(when: datetime | date, anchor_day: int) -> str:
    """Return the administrative billing month containing *when* (UTC)."""
    if isinstance(when, datetime):
        moment = when.astimezone(UTC)
        day = moment.date()
    else:
        day = when
    month = f"{day.year:04d}-{day.month:02d}"
    return month if day >= _period_start(month, anchor_day).date() else _previous_month(month)


def _iter_months(first: str, last: str):
    month = first
    while month <= last:
        yield month
        month = _next_month(month)


def _cancellation_end(config: sqlite3.Row, now: datetime, anchor_day: int,
                      account_type: str) -> datetime:
    """`deleted_at` a plan/agent key or account should receive on cancellation.

    api accounts are always terminated immediately (no subscription lifecycle).
    For subscription types the configured default deletion operation decides:
      'immediate'     → deleted_at = now (本期计费, 立即停止路由).
      'end_of_period' → deleted_at = end of the current billing period
                        (本期计费, 下期不计费); the entity keeps routing until
                        then because a future deleted_at is treated as active.
    `_billing_period_month(end, anchor_day)` must still equal the current
    period, hence the -1s before the next period's start.
    """
    if deletion_policy(account_type) == "immediate" or config["cancellation_mode"] == "immediate":
        return now
    current = _billing_period_month(now, anchor_day)
    return _period_start(_next_month(current), anchor_day) - timedelta(seconds=1)



__all__ = [name for name in globals() if not name.startswith('__')]
