"""Observed upstream usage for subtype accounts (poll + billing input).

Some subscription upstreams (e.g. OpenCode Go) publish their own rolling,
weekly and monthly counters and reset the monthly window on a boundary the
local calendar cannot predict.  This service queries the usage endpoint
declared by the account-type spec and stores one snapshot per credential in
``upstream_credentials.usage_json``; ``app/services/billing_units.py`` turns
the monthly part into the reset-driven billing period.

Every query is defensive: a malformed window is dropped, and an unreachable
or rejecting endpoint keeps the previous snapshot while recording an error
string, so a failed poll never advances the observed monthly chain.  Billing
treats a snapshot older than 48 hours, one carrying an error, or one without
a complete observed monthly period as absent and falls back to the account's
anchor day.

The usage route is first-party but undocumented, so every deviation from the
expected shape — a missing window, an unparseable percent or resetsAt, a
missing monthly boundary, transport or HTTP failures — is logged at warning
level with the credential and a body preview.  An upstream shape change must
be visible in the logs instead of only degrading the snapshot.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import requests

from app.core.time import (
    as_utc, format_utc, parse_external_timestamp, parse_runtime_timestamp,
    utc_now,
)
from app.db.proxy.key_usage import record_usage_snapshots, usage_targets

log = logging.getLogger(__name__)

USAGE_REQUEST_TIMEOUT_SECONDS = 15.0

# Upstream response key -> stored window name.
WINDOW_SOURCES = (("rolling", "5h"), ("weekly", "week"), ("monthly", "month"))


def _percent(value: object) -> int | None:
    """Coerce a reported percentage to a clamped integer; None when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(0, min(100, int(number)))


def _resets_at(value: object) -> datetime | None:
    """Parse an ISO-8601 string or epoch number reset stamp; None if invalid."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return parse_external_timestamp(value)
    except (TypeError, ValueError):
        return None


def _preview(value: object, limit: int = 80) -> str:
    """A size-bounded render of any JSON-ish value for log messages."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def parse_usage_body(body: object, *, context: str = "") -> tuple[dict, datetime | None]:
    """Extract stored windows and the raw monthly reset from a response body.

    The raw monthly ``resetsAt`` is returned separately because the observed
    billing period chain needs it even when the reported percentage is zero
    (the upstream then returns a now+duration placeholder that must not be
    stored as a window reset).

    Structural deviations never abort the poll, but each one is logged so a
    shape change at the undocumented upstream surface is noticeable; without
    the log a reshaping endpoint would silently degrade the snapshot.
    """
    prefix = f"usage poll: {context} " if context else "usage poll: "
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        raise ValueError("响应缺少 usage 字段")
    windows: dict[str, dict] = {}
    for source, name in WINDOW_SOURCES:
        raw = usage.get(source)
        if not isinstance(raw, dict):
            log.warning("%swindow '%s' missing or not an object: %s",
                        prefix, source, _preview(raw))
            continue
        percent = _percent(raw.get("percent"))
        if percent is None:
            log.warning("%swindow '%s' has an unusable percent: %s",
                        prefix, source, _preview(raw.get("percent")))
            continue
        resets = _resets_at(raw.get("resetsAt"))
        if resets is None and percent > 0:
            log.warning("%swindow '%s' has an unparseable resetsAt: %s",
                        prefix, source, _preview(raw.get("resetsAt")))
        # A zero percentage reports a now+duration placeholder, not a reset.
        windows[name] = {
            "percent": percent,
            "resets_at": (format_utc(resets)
                          if resets is not None and percent > 0 else None),
        }
    monthly = usage.get("monthly")
    monthly_resets = (_resets_at(monthly.get("resetsAt"))
                      if isinstance(monthly, dict) else None)
    return windows, monthly_resets


def _timestamp(value: object) -> datetime | None:
    """Parse one optional runtime timestamp, ignoring malformed values."""
    try:
        return parse_runtime_timestamp(value)
    except ValueError:
        return None


def advance_monthly_period(prior: object, moment: datetime,
                           resets: datetime | None) -> dict | None:
    """Roll the observed monthly period chain forward by one observation.

    The chain advances only through boundaries the upstream reported: the new
    period starts at the previous end once *moment* has reached it, and keeps
    the previous start until then.  A reported reset earlier than the new
    start cannot shorten a period, so the previous end is kept.  A first
    observation that reports no monthly boundary establishes nothing: it must
    not anchor the first period to whenever the poller happened to run.
    """
    prior_start = prior_end = None
    if isinstance(prior, dict):
        prior_start = _timestamp(prior.get("start"))
        prior_end = _timestamp(prior.get("end"))
    if prior_start is None:
        if resets is None:
            return None
        start = as_utc(moment)
        end = resets
    else:
        start = (prior_end if prior_end is not None and moment >= prior_end
                 else prior_start)
        end = resets if resets is not None and resets > start else prior_end
    return {"start": format_utc(start),
            "end": format_utc(end) if end is not None else None}


def _prior_payload(raw: object) -> object:
    """Parse a stored usage_json column into the previous payload object."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _failure(prior: object, moment: datetime, message: str) -> dict:
    """A failed poll keeps the previous windows and monthly period."""
    previous = prior if isinstance(prior, dict) else {}
    return {
        "checked_at": format_utc(moment),
        "error": message,
        "windows": previous.get("windows") or {},
        "monthly_period": previous.get("monthly_period"),
    }


def _query_usage(url: str, key: str, *, moment: datetime, prior: object,
                 context: str = "") -> dict:
    """Query one credential and build the payload stored for it."""
    try:
        response = requests.get(
            url, headers={"Authorization": "Bearer " + key,
                          "Accept": "application/json"},
            timeout=USAGE_REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return _failure(prior, moment, f"网络错误：{type(exc).__name__}")
    if response.status_code != 200:
        return _failure(prior, moment, f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return _failure(prior, moment, "响应不是有效 JSON")
    try:
        windows, resets = parse_usage_body(body, context=context)
    except ValueError as exc:
        return _failure(prior, moment, str(exc))
    if not windows:
        log.warning("usage poll: %s response carried no usable windows: %s",
                    context, _preview(body, 300))
    if resets is None:
        log.warning("usage poll: %s response carried no usable monthly reset: %s",
                    context, _preview(body, 300))
    prior_month = prior.get("monthly_period") if isinstance(prior, dict) else None
    return {
        "checked_at": format_utc(moment),
        "error": None,
        "windows": windows,
        "monthly_period": advance_monthly_period(prior_month, moment, resets),
    }


def poll_usage_once(proxy_db, *, now: datetime | None = None) -> dict:
    """Refresh the usage snapshot of every usage-capable upstream key once.

    The database is read first and written once at the end, so the HTTP
    queries never run while a SQLite write lock is held.
    """
    moment = as_utc(now or utc_now()).replace(microsecond=0)
    conn = proxy_db._connect()
    try:
        targets = usage_targets(conn, moment=moment)
    finally:
        conn.close()
    snapshots: dict[str, dict] = {}
    errors = 0
    for target in targets:
        for credential in target["credentials"]:
            context = f"credential {credential['uuid']} ({target['url']})"
            payload = _query_usage(
                target["url"], credential["secret_value"], moment=moment,
                prior=_prior_payload(credential["usage_json"]), context=context)
            snapshots[credential["uuid"]] = payload
            if payload["error"] is not None:
                errors += 1
                log.warning("usage poll: %s failed: %s", context,
                            payload["error"])
    record_usage_snapshots(proxy_db.db_path, snapshots)
    if errors:
        # Billing silently reverts to the anchor calendar for a key whose
        # snapshot is stale or errored, so failures must be visible.
        log.warning("usage poll: %d/%d credentials failed", errors, len(snapshots))
    return {"upstreams": len(targets), "credentials": len(snapshots),
            "errors": errors}


__all__ = [
    "USAGE_REQUEST_TIMEOUT_SECONDS", "WINDOW_SOURCES", "advance_monthly_period",
    "parse_usage_body", "poll_usage_once",
]
