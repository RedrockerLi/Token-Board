"""Subscription (plan/agent) FX locking at the billing period start date.

Covers the period_start-lock contract:
  * a USD row locks as soon as fx_rates has an exact row for period_start
    (fx_rate_date == period_start afterwards; rate never re-fetched, even
    when later daily rates arrive or prices change);
  * missing historical rates are fetched on demand via ?date=period_start;
  * fetch failure degrades to a provisional (unlocked) value that keeps
    retrying and is never finalized until locked;
  * CNY rows pass through untouched; frozen rows are never touched.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest import mock

from app.db.proxy.billing import materialize_period_charges
from app.services import fx

from app.tests.support import AppDatabaseTestCase


class _FakeResponse:
    def __init__(self, payload=None, error: Exception | None = None):
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict:
        return self._payload


def _at() -> datetime:
    return datetime(2026, 7, 20, tzinfo=timezone.utc)


def _rates(conn, period_start: str) -> tuple:
    return conn.execute(
        "SELECT recurring_charge,currency,normalized_recurring_cost,"
        "fx_rate_date,finalized_at FROM billing_period_charges "
        "WHERE period_start=?", (period_start,)
    ).fetchone()


class BillingFxLockTest(AppDatabaseTestCase):
    def _usd_plan(self, db, valid_from: str = "2026-07-15",
                  price: float = 10.0, account_type: str = "plan",
                  name: str = "usd-plan") -> int:
        data = {
            "name": name, "account_type": account_type, "currency": "USD",
            "monthly_price": price, "valid_from": valid_from,
        }
        if account_type == "plan":
            data.update({
                "base_url": "http://example.test", "upstream_keys": ["sk-usd"],
                "new_valid_froms": [valid_from],
            })
        account_id = db.create_account(data)
        # create_account stamps effective_at with real wall-clock now, which
        # lies after the fixed timestamps these tests materialize at; move the
        # price event into the past so _rate() finds it (and a later
        # update_account can insert a fresh row with a unique effective_at).
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_rate_events SET effective_at='1990-01-01T00:00:00Z'")
        return account_id

    def _seed_rate(self, date: str, rate: float) -> None:
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
                "VALUES('USD','CNY',?,?)", (date, rate))

    def test_current_period_locks_on_period_start_rate(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db)
        self._seed_rate("2026-07-15", 7.0)
        with mock.patch("app.services.fx.requests.get") as get:
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "2026-07-15T00:00:00Z")
                self.assertEqual(row[:3], (10.0, "USD", 70.0))
                self.assertEqual(row[3], "2026-07-15")  # fx_rate_date == period_start
                self.assertIsNone(row[4])
                # Later daily rates arrive — the locked value must not move.
                conn.execute(
                    "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
                    "VALUES('USD','CNY','2026-07-18',7.2),('USD','CNY','2026-07-20',7.4)")
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                self.assertEqual(_rates(conn, "2026-07-15T00:00:00Z")[:3],
                                 (10.0, "USD", 70.0))
            get.assert_not_called()  # locked rows never touch the network

    def test_price_change_reuses_locked_rate(self) -> None:
        db = self.proxy_database()
        account_id = self._usd_plan(db)
        self._seed_rate("2026-07-15", 7.0)
        with mock.patch("app.services.fx.requests.get") as get:
            materialize_period_charges(str(self.proxy_path), _at())
            self.assertTrue(db.update_account(account_id, {"monthly_price": 12}))
            # update_account stamps effective_at with real wall-clock now,
            # which lies after _at(); move it inside the window so _rate()
            # sees the immediate price change.
            with sqlite3.connect(self.proxy_path) as conn:
                conn.execute(
                    "UPDATE billing_rate_events "
                    "SET effective_at='2026-07-01T00:00:00Z' "
                    "WHERE recurring_price=12")
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "2026-07-15T00:00:00Z")
                self.assertEqual(row[:3], (12.0, "USD", 84.0))  # 12 × locked 7.0
                self.assertEqual(row[3], "2026-07-15")
            get.assert_not_called()

    def test_historical_fetch_stores_period_start_exact_row(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db)
        payload = {"date": "2026-07-15", "rate": 6.9}
        with mock.patch("app.services.fx.requests.get",
                        return_value=_FakeResponse(payload)) as get:
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                self.assertEqual(_rates(conn, "2026-07-15T00:00:00Z")[:3],
                                 (10.0, "USD", 69.0))
                self.assertEqual(_rates(conn, "2026-07-15T00:00:00Z")[3],
                                 "2026-07-15")
                row = conn.execute(
                    "SELECT rate FROM fx_rates WHERE base_currency='USD' "
                    "AND quote_currency='CNY' AND date='2026-07-15'").fetchone()
                self.assertEqual(row, (6.9,))
            # A second run finds the exact row and must not fetch again.
            materialize_period_charges(str(self.proxy_path), _at())
            get.assert_called_once()
            self.assertEqual(get.call_args.kwargs["params"],
                             {"date": "2026-07-15"})

    def test_fetch_failure_stays_unlocked_and_retries_then_finalizes(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db, valid_from="2026-06-15")
        self._seed_rate("2026-07-01", 6.5)  # fallback for the provisional
        with mock.patch("app.services.fx.requests.get",
                        side_effect=RuntimeError("offline")):
            materialize_period_charges(str(self.proxy_path),
                                       datetime(2026, 7, 20, tzinfo=timezone.utc))
            with sqlite3.connect(self.proxy_path) as conn:
                # Closed period (06-15 → 07-15) on a degraded rate: provisional,
                # must NOT be finalized while unlocked.
                row = _rates(conn, "2026-06-15T00:00:00Z")
                self.assertEqual(row[:3], (10.0, "USD", 65.0))
                self.assertNotEqual(row[3], "2026-06-15")
                self.assertIsNone(row[4])
        # Network recovers: the same run locks and finalizes the closed period.
        with mock.patch("app.services.fx.requests.get",
                        return_value=_FakeResponse(
                            {"date": "2026-06-15", "rate": 6.9})):
            materialize_period_charges(str(self.proxy_path),
                                       datetime(2026, 7, 20, tzinfo=timezone.utc))
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "2026-06-15T00:00:00Z")
                self.assertEqual(row[:3], (10.0, "USD", 69.0))
                self.assertEqual(row[3], "2026-06-15")
                self.assertIsNotNone(row[4])  # finalized

    def test_existing_unfrozen_rows_are_corrected_and_frozen_rows_untouched(
            self) -> None:
        db = self.proxy_database()
        account_id = self._usd_plan(db)
        self._seed_rate("2026-07-15", 7.0)
        with sqlite3.connect(self.proxy_path) as conn:
            contract_id = conn.execute(
                "SELECT bc.id FROM billing_contracts bc JOIN accounts a "
                "ON a.id=bc.account_id WHERE a.id=?", (account_id,)
            ).fetchone()[0]
            cred_uuid = conn.execute(
                "SELECT uuid FROM upstream_credentials LIMIT 1").fetchone()[0]
            # Legacy-rule row: current period written with today's rate.
            conn.execute(
                "INSERT INTO billing_period_charges"
                "(contract_id,credential_uuid,period_start,period_end,"
                "recurring_charge,currency,normalized_recurring_cost,"
                "base_currency,fx_rate_date) VALUES(?,?,?,?,?,?,?,?,?)",
                (contract_id, cred_uuid, "2026-07-15T00:00:00Z",
                 "2026-08-15T00:00:00Z", 10.0, "USD", 74.0, "CNY",
                 "2026-07-20"))
            # Closed frozen row in the old rule: must never be rewritten.
            conn.execute(
                "INSERT INTO billing_period_charges"
                "(contract_id,credential_uuid,period_start,period_end,"
                "recurring_charge,currency,normalized_recurring_cost,"
                "base_currency,fx_rate_date,finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (contract_id, cred_uuid, "2026-06-15T00:00:00Z",
                 "2026-07-15T00:00:00Z", 10.0, "USD", 66.0, "CNY",
                 "2026-06-20", "2026-07-16T00:00:00Z"))
        with mock.patch("app.services.fx.requests.get") as get:
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                current = _rates(conn, "2026-07-15T00:00:00Z")
                self.assertEqual(current[:3], (10.0, "USD", 70.0))
                self.assertEqual(current[3], "2026-07-15")  # corrected + locked
                frozen = _rates(conn, "2026-06-15T00:00:00Z")
                self.assertEqual(frozen[:3], (10.0, "USD", 66.0))
                self.assertEqual(frozen[3], "2026-06-20")  # untouched
            get.assert_not_called()

    def test_cny_passthrough_and_finalization(self) -> None:
        db = self.proxy_database()
        db.create_account({
            "name": "cny-plan", "account_type": "plan", "currency": "CNY",
            "monthly_price": 20, "valid_from": "2026-07-15",
            "base_url": "http://example.test", "upstream_keys": ["sk-cny"],
            "new_valid_froms": ["2026-07-15"],
        })
        materialize_period_charges(str(self.proxy_path),
                                   datetime(2026, 8, 20, tzinfo=timezone.utc))
        with sqlite3.connect(self.proxy_path) as conn:
            closed = _rates(conn, "2026-07-15T00:00:00Z")
            self.assertEqual(closed[:3], (20.0, "CNY", 20.0))
            self.assertIsNone(closed[3])       # no fx_rate_date for CNY
            self.assertIsNotNone(closed[4])    # finalized at period end
            current = _rates(conn, "2026-08-15T00:00:00Z")
            self.assertEqual(current[:3], (20.0, "CNY", 20.0))
            self.assertIsNone(current[4])      # current period not finalized

    def test_weekend_period_start_echoes_requested_date(self) -> None:
        # 2026-07-18 is a Saturday; v2 echoes the requested date with the
        # last trading day's rate, which is exactly the locked row we need.
        db = self.proxy_database()
        self._usd_plan(db, valid_from="2026-07-18")
        with mock.patch("app.services.fx.requests.get",
                        return_value=_FakeResponse(
                            {"date": "2026-07-18", "rate": 7.1})):
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "2026-07-18T00:00:00Z")
                self.assertEqual(row[:3], (10.0, "USD", 71.0))
                self.assertEqual(row[3], "2026-07-18")

    def test_pre_1999_period_start_never_hits_the_api(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db, valid_from="1998-12-15")
        self._seed_rate("1999-01-01", 6.5)
        with mock.patch("app.services.fx.requests.get",
                        side_effect=RuntimeError("offline")) as get:
            materialize_period_charges(
                str(self.proxy_path),
                datetime(1999, 2, 20, tzinfo=timezone.utc))
            requested = [c.kwargs["params"]["date"] for c in get.call_args_list]
            self.assertTrue(requested)  # 1999-01-15 period did try
            self.assertTrue(all(d >= "1999-01-01" for d in requested))
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "1998-12-15T00:00:00Z")
                self.assertIsNone(row[4])  # never finalized while unlocked
                self.assertNotEqual(row[3], "1998-12-15")

    def test_period_start_before_earliest_stored_rate_uses_earliest(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db)
        self._seed_rate("2026-08-01", 6.8)
        with mock.patch("app.services.fx.requests.get",
                        side_effect=RuntimeError("offline")):
            materialize_period_charges(str(self.proxy_path), _at())
            with sqlite3.connect(self.proxy_path) as conn:
                row = _rates(conn, "2026-07-15T00:00:00Z")
                # 6.8 from the earliest stored row, never 1.0, and unlocked.
                self.assertEqual(row[:3], (10.0, "USD", 68.0))
                self.assertEqual(row[3], "2026-08-01")
                self.assertIsNone(row[4])

    def test_agent_account_locks_like_plan(self) -> None:
        db = self.proxy_database()
        self._usd_plan(db, valid_from="2026-07-15", account_type="agent",
                       name="codex-sub")
        self._seed_rate("2026-07-15", 7.0)
        materialize_period_charges(str(self.proxy_path), _at())
        with sqlite3.connect(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT credential_uuid,recurring_charge,currency,"
                "normalized_recurring_cost,fx_rate_date FROM billing_period_charges"
            ).fetchone()
            self.assertIsNone(row[0])  # per-account unit, no credential
            self.assertEqual(row[1:4], (10.0, "USD", 70.0))
            self.assertEqual(row[4], "2026-07-15")

    def test_ensure_rate_historical_fetch_and_guards(self) -> None:
        self.proxy_database()  # initialize the schema
        with sqlite3.connect(self.proxy_path) as conn:
            conn.row_factory = sqlite3.Row  # get_rate reads row["rate"]
            payload = {"date": "2026-07-15", "rate": 7.1}
            with mock.patch("app.services.fx.requests.get",
                            return_value=_FakeResponse(payload)) as get:
                self.assertEqual(fx.ensure_rate(conn, date="2026-07-15"), 7.1)
                get.assert_called_once_with(
                    fx.FRANKFURTER_URL, timeout=3,
                    params={"date": "2026-07-15"})
                self.assertEqual(tuple(conn.execute(
                    "SELECT date,rate FROM fx_rates WHERE base_currency='USD' "
                    "AND quote_currency='CNY'").fetchone()), ("2026-07-15", 7.1))
                # Exact row present: no second fetch.
                self.assertEqual(fx.ensure_rate(conn, date="2026-07-15"), 7.1)
                get.assert_called_once()
            # Pre-1999 dates never hit the API.
            conn.execute(
                "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
                "VALUES('USD','CNY','1999-01-01',6.5)")
            with mock.patch("app.services.fx.requests.get") as get:
                self.assertEqual(fx.ensure_rate(conn, date="1998-01-15"), 6.5)
                get.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
