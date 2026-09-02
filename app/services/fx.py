"""Daily USD→CNY exchange-rate cache — local-only, never synced to cloud.

Fetch-on-demand rule (per requirements):
  * If the rate for the requested date is already stored → use it directly.
  * If it is missing → fetch from frankfurter.dev (with ?date= when a
    historical date is requested) and store it.
  * If the fetch fails (or returns the same old data) → keep using the most
    recent stored rate.

Two callers share this module: the daily fx-prewarm fetches only today's
rate (no ?date=), while the subscription materializer requests the rate of
each billing period's start date (?date=YYYY-MM-DD, supported by
frankfurter from 1999-01-04 on).

The table lives in token-board.db (fx_rates) and is excluded from cloud sync.
Every function is best-effort and never raises into the request path. Generic
readers retain the historical 1.0 result when a pair has never been stored;
the billing materializer treats that ``source_date is None`` state as
unresolved and does not finalize a financial charge with it.
"""

from dataclasses import dataclass
import logging
from collections.abc import Callable

import requests

from app.core.time import utc_now

FRANKFURTER_URL = "https://api.frankfurter.dev/v2/rate/USD/CNY"
_FETCH_TIMEOUT = 3  # seconds
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FxResolution:
    """Immutable result of resolving a currency pair for a target date."""

    rate: float
    source_date: str | None
    exact: bool
    locked: bool


class FxRateResolver:
    """Read-only FX resolution with an explicit provisional state.

    ``resolve`` never fetches or writes. ``ensure`` is the opt-in boundary for
    historical fetches and returns the same immutable result type. ``locked``
    still identifies an exact period-start rate; billing may explicitly freeze
    a non-exact fallback when the configured policy allows it.
    """

    @staticmethod
    def resolve(conn, base: str, quote: str, target_date: str) -> FxResolution:
        if base == quote:
            return FxResolution(1.0, None, True, True)
        exact = conn.execute(
            "SELECT rate,date FROM fx_rates WHERE base_currency=? "
            "AND quote_currency=? AND date=?", (base, quote, target_date)
        ).fetchone()
        if exact is not None:
            return FxResolution(float(exact["rate"]), str(exact["date"]), True, True)
        row = conn.execute(
            "SELECT rate,date FROM fx_rates WHERE base_currency=? "
            "AND quote_currency=? AND date<=? ORDER BY date DESC LIMIT 1",
            (base, quote, target_date),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT rate,date FROM fx_rates WHERE base_currency=? "
                "AND quote_currency=? ORDER BY date ASC LIMIT 1",
                (base, quote),
            ).fetchone()
        if row is None:
            return FxResolution(1.0, None, False, False)
        return FxResolution(float(row["rate"]), str(row["date"]), False, False)

    @classmethod
    def ensure(cls, conn, base: str, quote: str, target_date: str) -> FxResolution:
        ensure_rate(conn, base, quote, date=target_date)
        return cls.resolve(conn, base, quote, target_date)

    @staticmethod
    def finalize(resolution: FxResolution) -> float:
        if not resolution.locked:
            raise ValueError("provisional FX resolution cannot be finalized")
        return resolution.rate


def get_rate(conn, base: str = "USD", quote: str = "CNY",
             date: str | None = None) -> float:
    """Nearest-latest stored rate for ``date`` (default today UTC), read-only.

    A rate that is not exactly for *date* falls back to the most recent earlier
    row.  When *date* precedes every stored rate (e.g. a past month before the
    first fetch), uses the earliest stored rate so past USD subscriptions are
    not silently undervalued. Only when the pair has never been stored does it
    return 1.0 for compatibility; billing finalization rejects that unresolved
    state instead of treating it as a real exchange rate.
    """
    date = date or utc_now().strftime("%Y-%m-%d")
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

    A historical ``date`` is fetched with ``?date=YYYY-MM-DD`` (frankfurter
    covers 1999-01-04 on; earlier dates skip the fetch entirely).  The stored
    row uses the response's own ``date`` — v2 echoes the requested date, so a
    weekend request stores the requested date with the last trading day's
    rate, which is what the period-start locking rule relies on.
    """
    date = date or utc_now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT rate FROM fx_rates WHERE base_currency=? AND quote_currency=? AND date=?",
        (base, quote, date)).fetchone()
    if row is not None:
        return float(row["rate"])
    if date < "1999-01-01":
        # frankfurter has no data before 1999; never hammer the API for a
        # period start that can never resolve.
        log.warning("FX date %s predates frankfurter coverage; skipping fetch", date)
        return get_rate(conn, base, quote, date)

    try:
        owns_write = not conn.in_transaction
        resp = requests.get(FRANKFURTER_URL, timeout=_FETCH_TIMEOUT,
                            params={"date": date})
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("rate"))
        fetched_date = data.get("date") or date
        conn.execute(
            "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) VALUES(?,?,?,?) "
            "ON CONFLICT(base_currency,quote_currency,date) DO UPDATE SET rate=excluded.rate",
            (base, quote, fetched_date, rate))
        # The caller may own a larger billing transaction.  Only commit when
        # this helper opened no transaction of its own; transaction ownership
        # must remain at the workflow boundary.
        if owns_write:
            conn.commit()
    except Exception as exc:
        # Offline/bad payload: use the nearest stored rate below, but keep the
        # failure visible to the billing health and application logs.
        log.warning("FX fetch failed; using stored rate: %s", exc)
        if on_error:
            on_error(exc)

    return get_rate(conn, base, quote, date)
