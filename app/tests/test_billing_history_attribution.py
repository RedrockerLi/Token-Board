"""History must survive deletes and renames: every report reads identities.

These tests pin the semantics that deletion only stops future imports/routing
and that ``name`` is a pure display attribute — no billing read may depend on
the live configuration graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.proxy.billing import materialize_period_charges

from app.tests.support import AppDatabaseTestCase, sqlite_connection


# Stats fields that are attributed history.  ``active_accounts`` and the
# billing-health fields intentionally stay live-configuration readings.
ATTRIBUTED_STATS = (
    "total_requests", "today_requests", "metered_cost", "recurring_cost",
    "total_cost", "today_cost", "theoretical_cost", "today_actual_cost",
    "total_tokens", "active_upstreams",
)


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class BillingHistoryAttributionTest(AppDatabaseTestCase):
    def _snapshot(self, db, now: datetime) -> dict:
        from app.services.billing_report import actual_cost
        with sqlite_connection(self.proxy_path) as conn:
            costs = actual_cost(conn, now=now)
        stats = db.get_stats()
        return {
            "costs": costs,
            "stats": {key: stats[key] for key in ATTRIBUTED_STATS},
            "daily": db.get_daily_billing(),
            "daily_by_model": db.get_daily_billing_by_model(),
            "summary": db.get_billing_summary(),
            "today_upstreams": db.get_today_upstream_usage(),
            "recent_days": db.get_recent_billing_days(),
        }

    def test_deleted_proxy_accounts_stay_in_every_billing_read(self) -> None:
        db = self.proxy_database()
        db.update_plan_billing_config({"cancellation_mode": "immediate"})
        api_id = db.create_account({
            "name": "hist-api", "account_type": "api",
            "base_url": "https://hist-api.example",
            "upstream_keys": ["hist-api-key"],
        })
        plan_id = db.create_account({
            "name": "hist-plan", "account_type": "plan", "monthly_price": 12,
            "currency": "CNY", "base_url": "https://hist-plan.example",
            "upstream_keys": ["hist-plan-key"],
        })
        now = datetime.now(timezone.utc).replace(microsecond=0)
        timestamp = _timestamp(now)
        with sqlite_connection(self.proxy_path) as conn:
            conn.executemany(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "account_identity_id,model,status_code,requested_at,total_tokens,"
                "equivalent_cost,billed_usage_cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    ("hist-api-request", "proxy", api_id, api_id, "model", 200,
                     timestamp, 1, 3, 3),
                    ("hist-plan-request", "proxy", plan_id, plan_id, "model", 200,
                     timestamp, 1, 5, 0),
                ],
            )
            conn.commit()

        before = self._snapshot(db, now)
        names = {row["account_name"] for row in before["today_upstreams"]}
        self.assertIn("hist-api", names)
        self.assertIn("hist-plan", names)
        self.assertAlmostEqual(before["costs"]["metered_cost"], 3.0)
        self.assertAlmostEqual(before["costs"]["recurring_cost"], 12.0)

        self.assertTrue(db.delete_account(api_id, mode="immediate")["ok"])
        self.assertTrue(db.delete_account(plan_id, mode="immediate")["ok"])

        after = self._snapshot(db, now)
        self.assertEqual(after, before)
        with sqlite_connection(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM accounts WHERE id IN (?,?)",
                (api_id, plan_id)).fetchone())
            # The delete keeps every historical request row.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE event_id IN "
                "('hist-api-request','hist-plan-request')").fetchone()[0], 2)

    def test_deleted_agent_software_stays_in_every_billing_read(self) -> None:
        db = self.proxy_database()
        subscription_id = db.create_agent_subscription({
            "name": "hist-subscription", "monthly_price": 15,
            "currency": "CNY",
        })
        software_id = db.create_agent_software({
            "name": "hist-agent", "agent_kind": "codex",
            "subscription_ids": [subscription_id],
        })
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertTrue(db.insert_agent_usage(
            software_id=software_id, model="hist-model",
            prompt_tokens=10, completion_tokens=5, cache_read_tokens=0,
            total_tokens=15, requested_at=_timestamp(now),
            event_id="hist-agent-event",
        ))

        before = self._snapshot(db, now)
        names = {row["account_name"] for row in before["today_upstreams"]}
        self.assertIn("hist-agent", names)
        self.assertAlmostEqual(before["costs"]["recurring_cost"], 15.0)

        self.assertTrue(db.delete_agent_software(software_id))

        after = self._snapshot(db, now)
        self.assertEqual(after, before)
        names = {row["account_name"] for row in after["today_upstreams"]}
        self.assertIn("hist-agent", names)
        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT account_id,account_identity_id FROM request_log "
                "WHERE event_id='hist-agent-event'").fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], software_id)

    def test_rename_is_not_a_billing_event(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "rename-target", "account_type": "plan",
            "monthly_price": 12, "currency": "CNY",
            "base_url": "https://rename.example",
            "upstream_keys": ["rename-key"],
        })

        def rate_rows() -> list[tuple]:
            with sqlite_connection(self.proxy_path) as conn:
                return [tuple(row) for row in conn.execute(
                    "SELECT recurring_price,effective_at,effective_rule "
                    "FROM billing_rate_events WHERE contract_id="
                    "(SELECT id FROM billing_contracts WHERE account_id=?) "
                    "ORDER BY id", (account_id,))]

        def latest_price() -> float:
            with sqlite_connection(self.proxy_path) as conn:
                row = conn.execute(
                    "SELECT recurring_price FROM billing_rate_events "
                    "WHERE contract_id="
                    "(SELECT id FROM billing_contracts WHERE account_id=?) "
                    "ORDER BY effective_at DESC,id DESC LIMIT 1",
                    (account_id,)).fetchone()
            return float(row[0])

        def export_event_count() -> int:
            with sqlite_connection(self.proxy_path) as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM billing_export_events "
                    "WHERE account_id=?", (account_id,)).fetchone()[0]

        stats_before = db.get_stats()
        daily_before = db.get_daily_billing()
        rates_before = rate_rows()
        events_before = export_event_count()

        # A rename-only save (the dialog always submits the current price)
        # writes no billing fact; the background materializer keeps working.
        for name in ("rename-target-2", "rename-target-3"):
            self.assertTrue(db.update_account(account_id, {
                "name": name, "account_type": "plan",
                "monthly_price": 12, "currency": "CNY",
            }))
        materialize_period_charges(self.proxy_path)

        self.assertEqual(rate_rows(), rates_before)
        self.assertEqual(export_event_count(), events_before)
        # The name is a display attribute and follows the rename; every
        # number stays identical.
        self.assertEqual(
            [dict(row, account_name=None) for row in db.get_daily_billing()],
            [dict(row, account_name=None) for row in daily_before])
        self.assertEqual(db.get_stats(), stats_before)
        with sqlite_connection(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM account_identities WHERE id=?",
                (account_id,)).fetchone()[0], "rename-target-3")

        # A real price change appends exactly one next-period event.
        self.assertTrue(db.update_account(account_id, {"monthly_price": 20}))
        self.assertEqual(len(rate_rows()), len(rates_before) + 1)
        self.assertEqual(latest_price(), 20.0)
        # Re-saving the same price stays a no-op.
        self.assertTrue(db.update_account(account_id, {"monthly_price": 20}))
        self.assertEqual(len(rate_rows()), len(rates_before) + 1)

    def test_same_second_price_saves_do_not_raise(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "same-second-plan", "account_type": "plan",
            "monthly_price": 10, "currency": "CNY",
            "base_url": "https://same-second.example",
            "upstream_keys": ["same-second-key"],
        })
        # Back-to-back saves inside one second used to hit the
        # UNIQUE(contract_id,effective_at,effective_rule) guard as a 500.
        self.assertTrue(db.update_account(account_id, {"monthly_price": 30}))
        self.assertTrue(db.update_account(account_id, {"monthly_price": 40}))
        with sqlite_connection(self.proxy_path) as conn:
            latest = conn.execute(
                "SELECT recurring_price FROM billing_rate_events "
                "WHERE contract_id="
                "(SELECT id FROM billing_contracts WHERE account_id=?) "
                "ORDER BY effective_at DESC,id DESC LIMIT 1",
                (account_id,)).fetchone()[0]
        self.assertEqual(float(latest), 40.0)

    def test_product_delete_does_not_trigger_log_retention(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "retention-api", "account_type": "api",
            "base_url": "https://retention.example",
            "upstream_keys": ["retention-key"],
        })
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with sqlite_connection(self.proxy_path) as conn:
            conn.executemany(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "account_identity_id,model,status_code,requested_at) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    ("retention-old", "proxy", account_id, account_id,
                     "model", 200, _timestamp(now - timedelta(days=40))),
                    ("retention-recent", "proxy", account_id, account_id,
                     "model", 200, _timestamp(now)),
                ],
            )
            conn.commit()
            mark = conn.execute(
                "SELECT MAX(id) FROM request_log").fetchone()[0]

        self.assertTrue(db.delete_account(account_id, mode="immediate")["ok"])
        with sqlite_connection(self.proxy_path) as conn:
            # A product delete never removes request history.
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE event_id IN "
                "('retention-old','retention-recent')").fetchone()[0], 2)

        # Only the exported 30-day-old row is removed by the retention pass.
        cleaned = db.cleanup_exported_logs(mark)
        self.assertEqual(cleaned, 1)
        with sqlite_connection(self.proxy_path) as conn:
            remaining = {row[0] for row in conn.execute(
                "SELECT event_id FROM request_log WHERE event_id IN "
                "('retention-old','retention-recent')")}
        self.assertEqual(remaining, {"retention-recent"})
