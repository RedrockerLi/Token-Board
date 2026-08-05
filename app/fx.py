"""Daily USD→CNY exchange-rate cache — local-only, never synced to cloud.

Fetch-on-demand rule (per requirements):
  * If today's rate is already stored → use it directly.
  * If today's rate is missing → fetch from frankfurter.dev and store it.
  * If the fetch fails (or returns the same old data) → keep using the most
    recent stored rate.

The table lives in proxy.db (fx_rate) and is excluded from cloud sync
(app/sync.py _RUNTIME_TABLES).  Every function is best-effort and never raises
into the request path; missing data degrades to the last known rate (1.0 when
no rate has ever been stored).
"""

from datetime import datetime, timezone

import requests

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/USD/CNY"
_FETCH_TIMEOUT = 3  # seconds


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_date() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def get_rate(conn, base: str = "USD", quote: str = "CNY",
             date: str | None = None) -> float:
    """Nearest-latest stored rate for ``date`` (default today UTC), read-only.

    Matches the requirement "如果拉取后依然是旧数据，就直接用旧数据": a rate that
    is not exactly for *date* falls back to the most recent earlier row.  1.0
    when nothing is stored (no failure — CNY prices are unaffected).
    """
    date = date or _utc_date()
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE base=? AND quote=? AND date<=? "
        "ORDER BY date DESC LIMIT 1",
        (base, quote, date),
    ).fetchone()
    return float(row["rate"]) if row is not None else 1.0


def ensure_rate(conn, base: str = "USD", quote: str = "CNY",
                date: str | None = None) -> float:
    """Return the rate for ``date`` (default today), fetching and storing it
    when the exact row is missing.  Falls back to the nearest stored rate on
    any error.  Never raises.
    """
    date = date or _utc_date()
    row = conn.execute(
        "SELECT rate FROM fx_rate WHERE base=? AND quote=? AND date=?",
        (base, quote, date),
    ).fetchone()
    if row is not None:
        return float(row["rate"])

    try:
        resp = requests.get(FRANKFURTER_URL, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("rate"))
        fetched_date = data.get("date") or date
        conn.execute(
            "INSERT OR REPLACE INTO fx_rate (base, quote, date, rate, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (base, quote, fetched_date, rate,
             _utc_now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    except Exception:
        pass  # offline / bad payload → use nearest stored rate below

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
