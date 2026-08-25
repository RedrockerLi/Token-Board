"""Explicit time contracts used by runtime code and external adapters.

The application has two intentionally different timestamp grammars.  Runtime
data is strict ISO-8601 UTC, while third-party agent stores are allowed to be
more forgiving.  Keeping the distinction here prevents each adapter from
inventing its own epoch heuristic or silently accepting legacy runtime data.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from numbers import Real

UTC = timezone.utc

# These thresholds are part of the external timestamp contract.  A value
# below one hundred billion is epoch seconds, below one hundred trillion is
# milliseconds, and larger values are microseconds.  The ranges cover normal
# dates while making accidental unit changes visible in tests.
EPOCH_MILLISECOND_THRESHOLD = 100_000_000_000
EPOCH_MICROSECOND_THRESHOLD = 100_000_000_000_000


def utc_now() -> datetime:
    """Return a second-precision, timezone-aware UTC wall clock value."""

    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime | date) -> datetime:
    """Normalize a datetime/date to an aware UTC datetime."""

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def format_utc(value: datetime | date) -> str:
    """Format a runtime timestamp using the canonical ``...T...Z`` form."""

    return as_utc(value).replace(microsecond=0).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def parse_runtime_timestamp(value: object) -> datetime | None:
    """Parse a strict runtime ISO timestamp and normalize it to UTC.

    ``None``/empty values remain optional.  The old SQLite space-separated
    form is intentionally rejected here; only external adapters may accept it.
    """

    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("runtime timestamp must be an ISO-8601 string")
    text = value
    if " " in text:
        raise ValueError(
            "legacy space-separated timestamps are not supported; "
            "expected ISO format (YYYY-MM-DDTHH:MM:SSZ)")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("runtime timestamp must be an ISO-8601 string") from exc
    return as_utc(parsed)


def _epoch_datetime(number: Real) -> datetime:
    magnitude = abs(float(number))
    if magnitude < EPOCH_MILLISECOND_THRESHOLD:
        seconds = float(number)
    elif magnitude < EPOCH_MICROSECOND_THRESHOLD:
        seconds = float(number) / 1_000.0
    else:
        seconds = float(number) / 1_000_000.0
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("external epoch timestamp is out of range") from exc


def parse_external_timestamp(value: object) -> datetime | None:
    """Parse timestamps found in external Agent stores.

    Accepted values are epoch seconds/milliseconds/microseconds, ISO values
    with an explicit offset, naive ISO values interpreted as UTC, and the
    legacy SQLite space separator.  The unit thresholds are centralized above
    so adapters cannot drift apart.
    """

    if value in (None, ""):
        return None
    if isinstance(value, Real) and not isinstance(value, bool):
        return _epoch_datetime(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.lstrip("+-").replace(".", "", 1).isdigit():
            return _epoch_datetime(float(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unsupported external timestamp: {value!r}") from exc
    return as_utc(parsed)


@dataclass(frozen=True)
class BillingPeriod:
    """Administrative billing period with an inclusive anchor contract."""

    start: datetime
    end: datetime
    anchor_day: int


def billing_period(moment: datetime | date, anchor_day: int) -> BillingPeriod:
    """Return the period containing *moment*, clamping month-end anchors."""

    if not 1 <= int(anchor_day) <= 31:
        raise ValueError("anchor_day must be between 1 and 31")
    current = as_utc(moment)
    day = min(anchor_day, calendar.monthrange(current.year, current.month)[1])
    start = datetime(current.year, current.month, day, tzinfo=UTC)
    if current < start:
        year, month = (current.year - 1, 12) if current.month == 1 else (
            current.year, current.month - 1)
        day = min(anchor_day, calendar.monthrange(year, month)[1])
        start = datetime(year, month, day, tzinfo=UTC)
    year, month = (start.year + 1, 1) if start.month == 12 else (
        start.year, start.month + 1)
    end = datetime(year, month, min(anchor_day, calendar.monthrange(year, month)[1]),
                   tzinfo=UTC)
    return BillingPeriod(start, end, int(anchor_day))


def next_billing_period(start: datetime, anchor_day: int) -> BillingPeriod:
    """Return the period following a known period start."""

    return billing_period(as_utc(start) + timedelta(days=32), anchor_day)


__all__ = [
    "UTC", "BillingPeriod", "EPOCH_MILLISECOND_THRESHOLD",
    "EPOCH_MICROSECOND_THRESHOLD", "as_utc", "billing_period",
    "format_utc", "next_billing_period", "parse_external_timestamp",
    "parse_runtime_timestamp", "utc_now",
]
