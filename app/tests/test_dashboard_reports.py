from __future__ import annotations

import sqlite3

from app import create_app

from app.tests.support import AppDatabaseTestCase


class DashboardReportsTest(AppDatabaseTestCase):
    """Golden-cost assertions for the dashboard report routes.

    These pin the canonical V2 ledger formula: actual = billed usage +
    normalized recurring, theoretical = equivalent cost, and a missing FX
    rate surfaces as billing_incomplete rather than a fabricated zero.
    """

    def setUp(self) -> None:
        super().setUp()
        database = self.proxy_database()
        self.api_account_id = database.create_account({
            "name": "report-account", "account_type": "api",
            "base_url": "http://report.test", "upstream_keys": ["sk-report"],
        })
        self.plan_account_id = database.create_account({
            "name": "report-plan", "account_type": "plan",
            "base_url": "http://report-plan.test",
            "monthly_price": 12, "valid_from": "2026-08-01",
            "upstream_keys": ["sk-report-plan"],
        })
        with sqlite3.connect(self.dashboard_path) as conn:
            conn.executemany(
                "INSERT INTO accounts(account_id,name,account_kind) VALUES(?,?,?)",
                [(self.api_account_id, "report-account", "proxy"),
                 (self.plan_account_id, "report-plan", "proxy")],
            )
            conn.execute(
                "INSERT INTO daily_usage(date,account_id,model,input_tokens,"
                "cache_tokens,output_tokens,request_count,equivalent_cost,"
                "billed_usage_cost) VALUES('2026-08-09',?,?,40,0,10,2,7.0,3.0)",
                (self.api_account_id, "model-a"))
            conn.execute(
                "INSERT INTO daily_usage(date,account_id,model,input_tokens,"
                "cache_tokens,output_tokens,request_count,equivalent_cost,"
                "billed_usage_cost) VALUES('2026-08-10',?,?,10,0,20,1,4.0,2.0)",
                (self.api_account_id, "model-b"))
            conn.execute(
                "INSERT INTO daily_usage(date,account_id,model,input_tokens,"
                "cache_tokens,output_tokens,request_count,equivalent_cost,"
                "billed_usage_cost) VALUES('2026-08-11',?,?,5,0,5,1,9.0,0.0)",
                (self.plan_account_id, "plan-model"))
            conn.execute(
                "INSERT INTO monthly_recurring_costs(period_start,account_id,"
                "billing_unit_id,recurring_charge,equivalent_cost,currency,"
                "normalized_recurring_cost,charge_frozen_at) "
                "VALUES('2026-08-01T00:00:00Z',?,?,12,9,'CNY',12,'2026-08-01T00:00:00Z')",
                (self.plan_account_id, "unit-1"))
            # FX normalization pending: NULL normalized cost must surface as
            # billing_incomplete instead of counting as zero.
            conn.execute(
                "INSERT INTO monthly_recurring_costs(period_start,account_id,"
                "billing_unit_id,recurring_charge,equivalent_cost,currency,"
                "normalized_recurring_cost) VALUES('2026-08-01T00:00:00Z',?,?,9,0,'USD',NULL)",
                (self.plan_account_id, "unit-2"))
            conn.commit()

        # The dashboard archive is not the source of truth for current
        # actual cost.  Keep matching live Token-Board facts in the fixture:
        # the archive rows above alone must not make a deleted/live decision.
        with sqlite3.connect(self.proxy_path) as conn:
            credential_uuid = conn.execute(
                "SELECT c.uuid FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id "
                "WHERE u.account_id=? ORDER BY c.uuid LIMIT 1",
                (self.plan_account_id,),
            ).fetchone()[0]
            contract_id = conn.execute(
                "SELECT id FROM billing_contracts WHERE account_id=? "
                "AND charge_type='recurring' AND billing_scope='credential'",
                (self.plan_account_id,),
            ).fetchone()[0]
            conn.executemany(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "model,status_code,requested_at,total_tokens,equivalent_cost,"
                "billed_usage_cost) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("report-live-1", "proxy", self.api_account_id,
                     "model-a", 200, "2026-08-09T12:00:00Z", 50, 3, 3),
                    ("report-live-2", "proxy", self.api_account_id,
                     "model-b", 200, "2026-08-10T12:00:00Z", 30, 2, 2),
                ],
            )
            conn.execute(
                "INSERT INTO billing_period_charges(contract_id,"
                "credential_uuid,period_start,period_end,recurring_charge,"
                "currency,normalized_recurring_cost,finalized_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (contract_id, credential_uuid, "2026-08-09T00:00:00Z",
                 "2026-09-09T00:00:00Z", 12, "CNY", 12,
                 "2026-08-09T00:00:00Z"),
            )
            conn.commit()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.app.config["DATA_STORE"].load()
        self.client = self.app.test_client()

    def test_summary_and_monthly_use_canonical_ledger(self) -> None:
        summary = self.client.get("/api/summary").get_json()
        # V2 total_cost is the actual ledger: metered usage (3.0 + 2.0) plus
        # the finalized recurring charge (12.0). Virtual plan usage remains
        # theoretical only.
        self.assertAlmostEqual(summary["total_cost"], 17.0)
        self.assertAlmostEqual(summary["metered_cost"], 5.0)
        self.assertAlmostEqual(summary["recurring_cost"], 12.0)
        self.assertAlmostEqual(summary["theoretical_cost"], 20.0)
        # actual = billed usage + normalized recurring (12), never + virtual.
        self.assertAlmostEqual(summary["actual_cost"], 17.0)
        self.assertEqual(summary["billing_incomplete_count"], 1)
        self.assertEqual(summary["billing_health"], "degraded")
        self.assertAlmostEqual(summary["plan_virtual_cost"], 9.0)
        self.assertAlmostEqual(
            summary["model_breakdown"]["model-a"]["theoretical_cost"], 7.0)
        self.assertAlmostEqual(
            summary["model_breakdown"]["model-b"]["theoretical_cost"], 4.0)
        self.assertAlmostEqual(
            summary["model_breakdown"]["plan-model"]["theoretical_cost"], 9.0)
        self.assertAlmostEqual(summary["model_breakdown"]["plan-model"]["cost"], 0.0)

        monthly = self.client.get("/api/monthly").get_json()
        aug = next(m for m in monthly if m["year"] == 2026 and m["month"] == 8)
        # `cost` keeps the historical api-equivalent meaning (7+4+9), while the
        # metered bill plus recurring subscription is `actual_cost`.
        self.assertAlmostEqual(aug["cost"], 20.0)
        self.assertAlmostEqual(aug["theoretical_cost"], 20.0)
        self.assertAlmostEqual(aug["actual_cost"], 17.0)

    def test_daily_and_breakdown_keep_costs_separate(self) -> None:
        daily = self.client.get("/api/daily?year=2026&month=8").get_json()
        self.assertEqual(daily["year"], 2026)
        self.assertEqual(daily["month"], 8)
        flattened = daily["days"]
        self.assertTrue(flattened)
        costs = sum(row["cost"] for row in flattened)
        theoretical = sum(row["theoretical_cost"] for row in flattened)
        self.assertAlmostEqual(costs, 17.0)
        self.assertAlmostEqual(theoretical, 20.0)

    def test_daily_model_filter_uses_virtual_cost_not_subscription(self) -> None:
        model_a = self.client.get(
            "/api/daily?year=2026&month=8&model=model-a").get_json()
        self.assertEqual(len(model_a["days"]), 1)
        row = model_a["days"][0]
        self.assertEqual(row["date"], "2026-08-09")
        # The model view is virtual/equivalent: it must never inherit the
        # account-level subscription fee that is anchored to 2026-08-01.
        self.assertAlmostEqual(row["cost"], 7.0)
        self.assertAlmostEqual(row["theoretical_cost"], 7.0)
        self.assertAlmostEqual(row["metered_cost"], 3.0)
        self.assertAlmostEqual(row["recurring_cost"], 0.0)
        self.assertAlmostEqual(row["actual_cost"], 3.0)

        plan_model = self.client.get(
            "/api/daily?year=2026&month=8&model=plan-model").get_json()
        self.assertEqual(len(plan_model["days"]), 1)
        row = plan_model["days"][0]
        self.assertEqual(row["date"], "2026-08-11")
        self.assertAlmostEqual(row["cost"], 9.0)
        self.assertAlmostEqual(row["theoretical_cost"], 9.0)
        self.assertAlmostEqual(row["metered_cost"], 0.0)
        self.assertAlmostEqual(row["recurring_cost"], 0.0)
        self.assertAlmostEqual(row["actual_cost"], 0.0)

    def test_frozen_dashboard_cost_does_not_change_after_live_delete(self) -> None:
        before = self.client.get("/api/summary").get_json()
        before_month = next(
            row for row in self.client.get("/api/monthly").get_json()
            if row["year"] == 2026 and row["month"] == 8
        )

        database = self.proxy_database()
        database.update_plan_billing_config({"cancellation_mode": "immediate"})
        self.assertTrue(database.delete_account(
            self.plan_account_id, mode="immediate")["ok"])

        # The live plan graph is gone, but Dashboard's already exported
        # frozen amount remains the same immutable historical fact.
        self.app.config["DATA_STORE"].load()
        after = self.client.get("/api/summary").get_json()
        after_month = next(
            row for row in self.client.get("/api/monthly").get_json()
            if row["year"] == 2026 and row["month"] == 8
        )
        self.assertAlmostEqual(after["recurring_cost"], before["recurring_cost"])
        self.assertAlmostEqual(after["actual_cost"], before["actual_cost"])
        self.assertAlmostEqual(after_month["actual_cost"], before_month["actual_cost"])

    def test_all_public_facades_import(self) -> None:
        from app.db.dashboard_db import DashboardDatabase
        from app.db.proxy_db import ProxyDatabase
        from app.db.proxy.facade import ProxyDatabase as Facade
        self.assertIs(ProxyDatabase, Facade)
        db = self.proxy_database()
        for method in ("get_accounts", "get_keys", "get_pricing",
                       "get_aggregates", "get_stats", "create_account",
                       "create_key", "create_pricing", "create_aggregate",
                       "update_account", "delete_account",
                       "get_billing_summary", "get_daily_billing",
                       "get_request_logs", "export_to_dashboard"):
            self.assertTrue(hasattr(db, method), method)
        self.assertTrue(callable(DashboardDatabase))
        self.assertTrue(hasattr(DashboardDatabase, "load_rows"))


if __name__ == "__main__":
    import unittest
    unittest.main()
