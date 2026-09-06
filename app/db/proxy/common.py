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

from app.core.time import (
    UTC,
    as_utc,
    billing_period,
    format_utc,
    parse_runtime_timestamp,
    utc_now,
)

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


class ConflictError(ValueError):
    """A valid request rejected because the live resource is in transition."""


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


def _subscription_date(value: object | None = None) -> str:
    """Return a subscription effective date; UTC midnight is implicit.

    The public form submits a calendar date.  Accepting an ISO timestamp here
    keeps older integrations compatible, but deliberately discards its clock
    portion: subscription starts are date-grained and always mean 00:00Z.
    """
    if value in (None, ""):
        return utc_now().date().isoformat()
    if isinstance(value, str) and "T" in value:
        parsed = parse_runtime_timestamp(value)
        if parsed is None:
            raise ValueError("订阅起始日必须是 YYYY-MM-DD")
        return parsed.date().isoformat()
    parsed = _parse_iso_date(value)
    if parsed is None:
        raise ValueError("订阅起始日必须是 YYYY-MM-DD")
    return parsed.isoformat()


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
    """Return a confirmed future ``ends_at`` for a subscription unit.

    API accounts are always terminated immediately. For subscriptions, the
    configured policy chooses immediate hard deletion or the current period's
    end boundary. The latter keeps the unit live and billable until the
    lifecycle finalizer physically deletes it.
    """
    if deletion_policy(account_type) == "immediate" or config["cancellation_mode"] == "immediate":
        return now
    current = _billing_period_month(now, anchor_day)
    return _period_start(_next_month(current), anchor_day) - timedelta(seconds=1)



__all__ = [
    "json", "os", "secrets", "sqlite3", "string", "urllib", "uuid",
    "monthrange", "date", "datetime", "timedelta", "timezone", "UTC",
    "as_utc", "billing_period", "format_utc", "ACCOUNT_TYPES",
    "deletion_policy", "type_holds_keys",
    "import_types", "is_routable", "is_subscription", "spec",
    "subscription_types", "_generate_key", "mask_key",
    "_parse_iso_date", "_period_start",
    "_previous_month", "_next_month", "_billing_period_month",
    "_iter_months", "_cancellation_end",
]
