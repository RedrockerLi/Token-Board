"""Billing periods resolved from the ledger, observed resets and the anchor."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date, datetime, timedelta, timezone

from app.core.time import billing_period, format_utc, parse_runtime_timestamp, utc_now
from app.db.proxy.billing import materialize_period_charges, resolve_unit_period
from app.services.billing_units import BillingUnit, SubscriptionState

from app.tests.support import AppDatabaseTestCase, sqlite_connection


class ResetDrivenBillingTest(AppDatabaseTestCase):
    def _plan(self, db, *, valid_from: str | None = None, price: float = 10,
              keys=("sk-go-alpha",), name: str = "go-plan",
              subtype: str | None = None) -> int:
        data = {
            "name": name, "account_type": "plan", "monthly_price": price,
            "currency": "CNY", "base_url": "http://example.test",
            "upstream_keys": list(keys),
        }
        if subtype is not None:
            data["subtype"] = subtype
        if valid_from is not None:
            data["valid_from"] = valid_from
            data["new_valid_froms"] = [valid_from] * len(keys)
        return db.create_account(data)

    def _set_cancellation_mode(self, mode: str) -> None:
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", mode))
            conn.commit()

    def _write_snapshot(self, account_id: int, payload: dict) -> None:
        """Store one usage_json snapshot for every key of the account."""
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE upstream_credentials SET usage_json=? WHERE upstream_id IN "
                "(SELECT id FROM upstreams WHERE account_id=?)",
                (json.dumps(payload, ensure_ascii=False), account_id))
            conn.commit()

    def _write_usage(self, account_id: int, *, checked_at: datetime,
                     start: datetime, end: datetime | None) -> None:
        """Store the snapshot the poller would have written for every key."""
        self._write_snapshot(account_id, {
            "checked_at": format_utc(checked_at),
            "error": None,
            "subscription": "active",
            "windows": {"month": {
                "percent": 40,
                "resets_at": format_utc(end) if end is not None else None}},
            "monthly_period": {
                "start": format_utc(start),
                "end": format_utc(end) if end is not None else None},
        })

    def _write_lapse(self, account_id: int, *, error_at: datetime,
                     checked_at: datetime | None = None,
                     monthly: dict | None = None) -> None:
        """Store the snapshot the poller writes while the subscription is gone."""
        payload = {
            "error": "HTTP 403",
            "error_at": format_utc(error_at),
            "subscription": "inactive",
            "windows": {},
            "monthly_period": monthly,
        }
        if checked_at is not None:
            payload["checked_at"] = format_utc(checked_at)
        self._write_snapshot(account_id, payload)

    def _charges(self, account_id: int) -> list[sqlite3.Row]:
        with sqlite_connection(self.proxy_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT c.credential_uuid,c.period_start,c.period_end,"
                "c.recurring_charge,c.currency,c.normalized_recurring_cost,"
                "c.finalized_at FROM billing_period_charges c "
                "JOIN billing_contracts bc ON bc.id=c.contract_id "
                "WHERE bc.account_id=? ORDER BY c.period_start,c.credential_uuid",
                (account_id,)).fetchall()

    def test_creation_bills_the_current_period_once_per_key(self) -> None:
        db = self.proxy_database()
        now = utc_now()
        account_id = self._plan(db, keys=("sk-go-alpha", "sk-go-bravo"))
        period = billing_period(now, now.day)

        rows = self._charges(account_id)

        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["period_start"], format_utc(period.start))
            self.assertEqual(row["period_end"], format_utc(period.end))
            self.assertAlmostEqual(row["recurring_charge"], 10.0)
            self.assertAlmostEqual(row["normalized_recurring_cost"], 10.0)
            self.assertIsNotNone(row["finalized_at"])

        materialize_period_charges(self.proxy_path, now)
        materialize_period_charges(self.proxy_path, now)

        self.assertEqual([row["period_start"] for row in self._charges(account_id)],
                         [format_utc(period.start)] * 2)

    def test_backdated_creation_still_bills_the_current_period(self) -> None:
        db = self.proxy_database()
        now = utc_now()
        valid_from = (now - timedelta(days=60)).date().isoformat()
        account_id = self._plan(db, valid_from=valid_from)
        period = billing_period(now, int(valid_from[-2:]))

        rows = self._charges(account_id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period_start"], format_utc(period.start))
        self.assertEqual(rows[0]["period_end"], format_utc(period.end))

    def test_future_start_is_not_billed_before_its_date(self) -> None:
        db = self.proxy_database()
        start = utc_now().date() + timedelta(days=10)
        account_id = self._plan(db, valid_from=start.isoformat())

        self.assertEqual(self._charges(account_id), [])

        moment = datetime.combine(start + timedelta(days=1), datetime.min.time(),
                                  tzinfo=timezone.utc)
        materialize_period_charges(self.proxy_path, moment)

        rows = self._charges(account_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period_start"], f"{start.isoformat()}T00:00:00Z")
        self.assertAlmostEqual(rows[0]["recurring_charge"], 10.0)

    def test_fresh_observation_rolls_from_the_ledger_end(self) -> None:
        db = self.proxy_database()
        account_id = self._plan(db)
        first = self._charges(account_id)[0]
        first_end = parse_runtime_timestamp(first["period_end"])
        moment = first_end + timedelta(hours=1)
        observed_end = first_end + timedelta(days=25)
        self._write_usage(account_id, checked_at=moment, start=first_end,
                          end=observed_end)

        materialize_period_charges(self.proxy_path, moment)

        rows = self._charges(account_id)
        self.assertEqual(
            [(row["period_start"], row["period_end"]) for row in rows],
            [(first["period_start"], first["period_end"]),
             (format_utc(first_end), format_utc(observed_end))])
        self.assertAlmostEqual(rows[1]["recurring_charge"], 10.0)
        self.assertIsNotNone(rows[1]["finalized_at"])

        materialize_period_charges(self.proxy_path, moment)
        self.assertEqual(len(self._charges(account_id)), 2)

    def test_stale_observation_falls_back_to_the_anchor_period(self) -> None:
        db = self.proxy_database()
        now = utc_now()
        account_id = self._plan(db)
        first = self._charges(account_id)[0]
        first_end = parse_runtime_timestamp(first["period_end"])
        moment = first_end + timedelta(hours=1)
        self._write_usage(account_id, checked_at=moment - timedelta(days=10),
                          start=first_end, end=first_end + timedelta(days=25))

        materialize_period_charges(self.proxy_path, moment)

        rows = self._charges(account_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["period_start"], format_utc(first_end))
        self.assertEqual(rows[1]["period_end"],
                         format_utc(billing_period(moment, now.day).end))

    def test_snapshot_without_a_monthly_end_falls_back_to_the_anchor(self) -> None:
        db = self.proxy_database()
        account_id = self._plan(db, valid_from="2026-08-01")
        moment = datetime(2026, 9, 15, tzinfo=timezone.utc)
        # A snapshot that names only a start cannot define a period.
        self._write_usage(account_id, checked_at=moment,
                          start=moment - timedelta(days=5), end=None)

        materialize_period_charges(self.proxy_path, moment)

        periods = {(row["period_start"], row["period_end"])
                   for row in self._charges(account_id)}
        self.assertIn(("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"), periods)
        # The snapshot's start must not become the period start.
        self.assertNotIn(("2026-09-10T00:00:00Z", "2026-10-01T00:00:00Z"),
                         periods)

    def test_observation_before_valid_from_is_floored(self) -> None:
        db = self.proxy_database()
        start = utc_now().date() + timedelta(days=5)
        account_id = self._plan(db, valid_from=start.isoformat())
        moment = datetime.combine(start + timedelta(days=1), datetime.min.time(),
                                  tzinfo=timezone.utc)
        observed_end = moment + timedelta(days=25)
        self._write_usage(account_id, checked_at=moment,
                          start=moment - timedelta(days=10), end=observed_end)

        materialize_period_charges(self.proxy_path, moment)

        rows = self._charges(account_id)
        self.assertEqual(len(rows), 1)
        # The upstream window may predate the paid term, but the charge may not.
        self.assertEqual(rows[0]["period_start"], f"{start.isoformat()}T00:00:00Z")
        self.assertEqual(rows[0]["period_end"], format_utc(observed_end))
        self.assertAlmostEqual(rows[0]["recurring_charge"], 10.0)

    def test_usage_tracked_creation_waits_for_the_first_observation(self) -> None:
        db = self.proxy_database()
        now = utc_now()
        account_id = self._plan(db, subtype="opencode-go")

        # The save triggers a poll; until any observation lands the anchor
        # must not bill the key — a subscription that is already gone would
        # otherwise get a creation charge.
        self.assertEqual(self._charges(account_id), [])
        materialize_period_charges(self.proxy_path, now + timedelta(minutes=10))
        self.assertEqual(self._charges(account_id), [])

        # No observation inside the grace → the anchor takes over as before.
        materialize_period_charges(self.proxy_path, now + timedelta(minutes=16))
        rows = self._charges(account_id)
        self.assertEqual(len(rows), 1)
        anchor = billing_period(now + timedelta(minutes=16), now.day)
        self.assertEqual(rows[0]["period_start"], format_utc(anchor.start))
        self.assertEqual(rows[0]["period_end"], format_utc(anchor.end))

        # A first observation inside the grace bills from the observed window.
        other = self._plan(db, subtype="opencode-go", keys=("sk-go-bravo",))
        observed = utc_now()
        self._write_usage(other, checked_at=observed, start=observed,
                          end=observed + timedelta(days=30))
        materialize_period_charges(self.proxy_path, observed + timedelta(minutes=1))
        rows = self._charges(other)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["period_start"], format_utc(observed))
        self.assertEqual(rows[0]["period_end"],
                         format_utc(observed + timedelta(days=30)))

    def test_lapse_verdict_stops_opening_periods(self) -> None:
        db = self.proxy_database()
        account_id = self._plan(db, subtype="opencode-go")
        t0 = utc_now()
        reset = t0 + timedelta(days=30)
        self._write_usage(account_id, checked_at=t0, start=t0, end=reset)
        materialize_period_charges(self.proxy_path, t0 + timedelta(minutes=1))
        self.assertEqual(len(self._charges(account_id)), 1)

        # The subscription is gone when the boundary passes; the poller keeps
        # confirming it (every failed poll refreshes error_at).
        self._write_lapse(
            account_id, error_at=reset + timedelta(hours=1), checked_at=t0,
            monthly={"start": format_utc(t0), "end": format_utc(reset)})
        materialize_period_charges(self.proxy_path,
                                   reset + timedelta(hours=1))
        materialize_period_charges(self.proxy_path,
                                   reset + timedelta(days=2))
        self.assertEqual(len(self._charges(account_id)), 1)

        # Once no poll refreshed the verdict for 48h it expires and billing
        # falls back to the anchor (the poller itself is down at that point).
        later = reset + timedelta(days=5)
        materialize_period_charges(self.proxy_path, later)
        rows = self._charges(account_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["period_start"], format_utc(reset))
        self.assertEqual(rows[1]["period_end"],
                         format_utc(billing_period(later, t0.day).end))

    def test_resumed_subscription_starts_a_new_chain_at_the_resume_moment(self) -> None:
        db = self.proxy_database()
        account_id = self._plan(db, subtype="opencode-go")
        t0 = utc_now()
        reset = t0 + timedelta(days=30)
        self._write_usage(account_id, checked_at=t0, start=t0, end=reset)
        materialize_period_charges(self.proxy_path, t0 + timedelta(minutes=1))

        # Lapse across the boundary: nothing opens while the poller keeps
        # confirming the verdict (fresh evidence within 48h).
        resumed = reset + timedelta(days=10)
        self._write_lapse(
            account_id, error_at=reset + timedelta(hours=1), checked_at=t0,
            monthly={"start": format_utc(t0), "end": format_utc(reset)})
        materialize_period_charges(self.proxy_path,
                                   reset + timedelta(hours=1, minutes=5))
        self.assertEqual(len(self._charges(account_id)), 1)
        self._write_lapse(
            account_id, error_at=resumed - timedelta(hours=1), checked_at=t0,
            monthly={"start": format_utc(t0), "end": format_utc(reset)})
        materialize_period_charges(self.proxy_path,
                                   resumed - timedelta(hours=1))
        self.assertEqual(len(self._charges(account_id)), 1)

        # The subscription is back: the new chain starts when it was first
        # seen again, not at the pre-lapse end the monthly chain still
        # carries, and the unpaid gap is never backfilled.
        second_reset = resumed + timedelta(days=30)
        self._write_snapshot(account_id, {
            "checked_at": format_utc(resumed),
            "error": None,
            "subscription": "active",
            "resumed_at": format_utc(resumed),
            "windows": {},
            "monthly_period": {"start": format_utc(reset),
                               "end": format_utc(second_reset)},
        })
        materialize_period_charges(self.proxy_path, resumed + timedelta(hours=1))
        rows = self._charges(account_id)
        self.assertEqual(
            [(row["period_start"], row["period_end"]) for row in rows],
            [(format_utc(t0), format_utc(reset)),
             (format_utc(resumed), format_utc(second_reset))])
        self.assertAlmostEqual(rows[1]["recurring_charge"], 10.0)

        # Later periods weld normally from the resumed chain's end; the
        # resume stamp no longer moves anything once the ledger leads.
        third_reset = second_reset + timedelta(days=30)
        self._write_snapshot(account_id, {
            "checked_at": format_utc(second_reset + timedelta(hours=1)),
            "error": None,
            "subscription": "active",
            "resumed_at": format_utc(resumed),
            "windows": {},
            "monthly_period": {"start": format_utc(second_reset),
                               "end": format_utc(third_reset)},
        })
        materialize_period_charges(self.proxy_path,
                                   second_reset + timedelta(hours=1))
        rows = self._charges(account_id)
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[2]["period_start"], rows[2]["period_end"]),
                         (format_utc(second_reset), format_utc(third_reset)))

    def test_resume_inside_a_covering_period_welds_at_its_end(self) -> None:
        """A resumed subscription under a frozen covering row welds at its end.

        That row was charged while the subscription was still believed
        active; the new chain cannot open before the recorded one ends (the
        accepted shape of the kept creation charges).
        """
        db = self.proxy_database()
        account_id = self._plan(db, subtype="opencode-go")
        t0 = utc_now()
        reset = t0 + timedelta(days=30)
        self._write_usage(account_id, checked_at=t0, start=t0, end=reset)
        materialize_period_charges(self.proxy_path, t0 + timedelta(minutes=1))

        resumed = t0 + timedelta(days=12)
        self._write_snapshot(account_id, {
            "checked_at": format_utc(resumed),
            "error": None,
            "subscription": "active",
            "resumed_at": format_utc(resumed),
            "windows": {},
            "monthly_period": {"start": format_utc(t0),
                               "end": format_utc(resumed + timedelta(days=30))},
        })
        materialize_period_charges(self.proxy_path, resumed + timedelta(hours=1))
        # The covering row is authoritative until it ends.
        self.assertEqual(len(self._charges(account_id)), 1)

        after = reset + timedelta(hours=1)
        self._write_snapshot(account_id, {
            "checked_at": format_utc(after),
            "error": None,
            "subscription": "active",
            "resumed_at": format_utc(resumed),
            "windows": {},
            "monthly_period": {"start": format_utc(reset),
                               "end": format_utc(reset + timedelta(days=30))},
        })
        materialize_period_charges(self.proxy_path, after)
        rows = self._charges(account_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["period_start"], rows[1]["period_end"]),
                         (format_utc(reset),
                          format_utc(reset + timedelta(days=30))))

    def test_cancellation_ignores_an_already_ended_observation(self) -> None:
        db = self.proxy_database()
        self._set_cancellation_mode("end_of_period")
        now = utc_now()
        account_id = self._plan(db)
        self._write_usage(account_id, checked_at=now,
                          start=now - timedelta(days=30),
                          end=now - timedelta(days=1))

        result = db.delete_account(account_id)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["deferred"], result)
        # An ended period cannot confirm a future end; the calendar decides.
        self.assertEqual(
            result["effective_ends_at"],
            format_utc(billing_period(now, now.day).end - timedelta(seconds=1)))

    def test_cancellation_uses_the_fresh_observed_end(self) -> None:
        db = self.proxy_database()
        self._set_cancellation_mode("end_of_period")
        account_id = self._plan(db)
        now = utc_now()
        observed_end = now + timedelta(days=20)
        self._write_usage(account_id, checked_at=now,
                          start=now - timedelta(days=10), end=observed_end)

        result = db.delete_account(account_id)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["deferred"], result)
        self.assertEqual(result["effective_ends_at"],
                         format_utc(observed_end - timedelta(seconds=1)))
        with sqlite_connection(self.proxy_path) as conn:
            ends_at = conn.execute(
                "SELECT ends_at FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?",
                (account_id,)).fetchone()[0]
        self.assertEqual(ends_at, format_utc(observed_end - timedelta(seconds=1)))

    def test_periods_roll_across_a_month_boundary_with_a_price_change(self) -> None:
        db = self.proxy_database()
        account_id = self._plan(db, valid_from="2026-08-01")

        materialize_period_charges(
            self.proxy_path, datetime(2026, 8, 2, tzinfo=timezone.utc))
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO billing_rate_events"
                "(contract_id,recurring_price,effective_at,effective_rule) "
                "SELECT id,12,'2026-09-01T00:00:00Z','next_period' "
                "FROM billing_contracts WHERE account_id=?", (account_id,))
            conn.commit()
        materialize_period_charges(
            self.proxy_path, datetime(2026, 9, 2, tzinfo=timezone.utc))

        periods = {row["period_start"]: row for row in self._charges(account_id)}
        first = periods["2026-08-01T00:00:00Z"]
        self.assertEqual(first["period_end"], "2026-09-01T00:00:00Z")
        self.assertAlmostEqual(first["recurring_charge"], 10.0)
        second = periods["2026-09-01T00:00:00Z"]
        self.assertEqual(second["period_end"], "2026-10-01T00:00:00Z")
        self.assertAlmostEqual(second["recurring_charge"], 12.0)
        self.assertAlmostEqual(second["normalized_recurring_cost"], 12.0)
        self.assertIsNotNone(second["finalized_at"])


class ResolveUnitPeriodAnchorTest(unittest.TestCase):
    """Direct checks of the resolver's anchor-branch edges."""

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE billing_period_charges ("
            "contract_id INTEGER, credential_uuid TEXT, "
            "period_start TEXT, period_end TEXT)")
        return conn

    def _unit(self, **overrides) -> BillingUnit:
        values = dict(
            billing_unit_id="credential:x", owner_id=1, account_id=1,
            owner_kind="proxy", charge_domain="proxy_credential",
            valid_from=date(2026, 8, 1), ends_at=None, currency="CNY",
            credential_uuid="x", contract_id=1)
        values.update(overrides)
        return BillingUnit(**values)

    def test_anchor_start_is_floored_at_resumed_at(self) -> None:
        resumed = datetime(2026, 9, 15, 6, 30, tzinfo=timezone.utc)
        moment = datetime(2026, 9, 20, tzinfo=timezone.utc)

        # Without a resume stamp the anchor boundary stands.
        period = resolve_unit_period(self._conn(), self._unit(), moment)
        self.assertEqual(period, (datetime(2026, 9, 1, tzinfo=timezone.utc),
                                  datetime(2026, 10, 1, tzinfo=timezone.utc)))

        # A subscription seen again later inside the same boundary must not
        # be backdated to its start (billing follows the observation).
        period = resolve_unit_period(
            self._conn(),
            self._unit(subscription=SubscriptionState(resumed_at=resumed)),
            moment)
        self.assertEqual(period, (resumed,
                                  datetime(2026, 10, 1, tzinfo=timezone.utc)))


if __name__ == "__main__":
    import unittest

    unittest.main()
