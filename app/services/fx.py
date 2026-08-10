"""Daily USD→CNY exchange-rate cache — local-only, never synced to cloud.

Fetch-on-demand rule (per requirements):
  * If today's rate is already stored → use it directly.
  * If today's rate is missing → fetch from frankfurter.dev and store it.
  * If the fetch fails (or returns the same old data) → keep using the most
    recent stored rate.

The table lives in proxy.db (fx_rates) and is excluded from cloud sync.
Every function is best-effort and never raises
into the request path; missing data degrades to the nearest stored rate (the
earliest one when the requested date precedes every row; 1.0 only when the
pair has never been stored).
"""

from datetime import datetime, timezone
import logging
from collections.abc import Callable

import requests

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/USD/CNY"
_FETCH_TIMEOUT = 3  # seconds
log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_date() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def get_rate(conn, base: str = "USD", quote: str = "CNY",
             date: str | None = None) -> float:
    """Nearest-latest stored rate for ``date`` (default today UTC), read-only.

    A rate that is not exactly for *date* falls back to the most recent earlier
    row.  When *date* precedes every stored rate (e.g. a past month before the
    first fetch), uses the earliest stored rate so past USD subscriptions are
    not silently undervalued.  Only when the pair has never been stored does it
    return 1.0 (no failure — CNY prices are unaffected).
    """
    date = date or _utc_date()
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE base_currency=? AND quote_currency=? AND date<=? "
        "ORDER BY date DESC LIMIT 1", (base, quote, date)).fetchone()
    if row is not None:
        return float(row["rate"])
    # 请求日期早于所有已存汇率（如过去月份早于首次拉取）→ 用最早一条。
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE base_currency=? AND quote_currency=? "
        "ORDER BY date ASC LIMIT 1", (base, quote)).fetchone()
    return float(row["rate"]) if row is not None else 1.0


def ensure_rate(conn, base: str = "USD", quote: str = "CNY",
                date: str | None = None,
                on_error: Callable[[Exception], None] | None = None) -> float:
    """Return the rate for ``date`` (default today), fetching and storing it
    when the exact row is missing.  Falls back to the nearest stored rate on
    any error.  Never raises.
    """
    date = date or _utc_date()
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE base_currency=? AND quote_currency=? AND date=?",
        (base, quote, date)).fetchone()
    if row is not None:
        return float(row["rate"])

    try:
        resp = requests.get(FRANKFURTER_URL, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("rate"))
        fetched_date = data.get("date") or date
        conn.execute(
            "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) VALUES(?,?,?,?) "
            "ON CONFLICT(base_currency,quote_currency,date) DO UPDATE SET rate=excluded.rate",
            (base, quote, fetched_date, rate))
        conn.commit()
    except Exception as exc:
        # Offline/bad payload: use the nearest stored rate below, but keep the
        # failure visible to the billing health and application logs.
        log.warning("FX fetch failed; using stored rate: %s", exc)
        if on_error:
            on_error(exc)

    return get_rate(conn, base, quote, date)


def rate_for_month(conn, month: str, base: str = "USD", quote: str = "CNY",
                   today: str | None = None) -> float:
    """Rate to apply when converting one billing month's subscription to CNY.

    Past months must stay frozen, so they use the nearest stored rate as of the
    month's first day (never re-fetched).  The current month (and any future
    month) refreshes with today's rate, fetching on demand.  ``month`` is
    ``YYYY-MM``.
    """
    today = today or _utc_date()
    if month >= today[:7]:
        return ensure_rate(conn, base, quote, today)
    return get_rate(conn, base, quote, month + "-01")
