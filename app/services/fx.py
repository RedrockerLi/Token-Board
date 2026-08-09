"""Daily USD→CNY exchange-rate cache — local-only, never synced to cloud.

Fetch-on-demand rule (per requirements):
  * If today's rate is already stored → use it directly.
  * If today's rate is missing → fetch from frankfurter.dev and store it.
  * If the fetch fails (or returns the same old data) → keep using the most
    recent stored rate.

The table lives in proxy.db (fx_rate) and is excluded from cloud sync
(app/sync.py _RUNTIME_TABLES).  Every function is best-effort and never raises
into the request path; missing data degrades to the nearest stored rate (the
earliest one when the requested date precedes every row; 1.0 only when the
pair has never been stored).
"""

from datetime import datetime, timezone

import requests

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/USD/CNY"
_FETCH_TIMEOUT = 3  # seconds


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_date() -> str:
    return _utc_now().strftime("%Y-%m-%d")


def _v1(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fx_rates'"
    ).fetchone() is not None


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
    table = "fx_rates" if _v1(conn) else "fx_rate"
    base_col = "base_currency" if table == "fx_rates" else "base"
    quote_col = "quote_currency" if table == "fx_rates" else "quote"
    row = conn.execute(
        f"SELECT rate FROM {table} WHERE {base_col}=? AND {quote_col}=? AND date<=? "
        "ORDER BY date DESC LIMIT 1", (base, quote, date)).fetchone()
    if row is not None:
        return float(row["rate"])
    # 请求日期早于所有已存汇率（如过去月份早于首次拉取）→ 用最早一条。
    row = conn.execute(
        f"SELECT rate FROM {table} WHERE {base_col}=? AND {quote_col}=? "
        "ORDER BY date ASC LIMIT 1", (base, quote)).fetchone()
    return float(row["rate"]) if row is not None else 1.0


def ensure_rate(conn, base: str = "USD", quote: str = "CNY",
                date: str | None = None) -> float:
    """Return the rate for ``date`` (default today), fetching and storing it
    when the exact row is missing.  Falls back to the nearest stored rate on
    any error.  Never raises.
    """
    date = date or _utc_date()
    is_v1 = _v1(conn)
    table = "fx_rates" if is_v1 else "fx_rate"
    base_col = "base_currency" if is_v1 else "base"
    quote_col = "quote_currency" if is_v1 else "quote"
    row = conn.execute(
        f"SELECT rate FROM {table} WHERE {base_col}=? AND {quote_col}=? AND date=?",
        (base, quote, date)).fetchone()
    if row is not None:
        return float(row["rate"])

    try:
        resp = requests.get(FRANKFURTER_URL, timeout=_FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("rate"))
        fetched_date = data.get("date") or date
        if is_v1:
            conn.execute(
                "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) VALUES(?,?,?,?) "
                "ON CONFLICT(base_currency,quote_currency,date) DO UPDATE SET rate=excluded.rate",
                (base, quote, fetched_date, rate))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO fx_rate (base, quote, date, rate, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (base, quote, fetched_date, rate,
                 _utc_now().strftime("%Y-%m-%d %H:%M:%S")))
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
