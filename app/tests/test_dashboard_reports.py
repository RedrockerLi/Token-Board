from __future__ import annotations

import sqlite3

from app import create_app
from app.db.dashboard_db import reconcile_accounts
from app.db.migrations import migrate

from app.tests.support import AppDatabaseTestCase


class DashboardReportsTest(AppDatabaseTestCase):
    """Golden-cost assertions for the dashboard report routes.

    These pin the canonical V1 ledger formula: actual = billed usage +
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
            "monthly_price": 12, "upstream_keys": ["sk-report-plan"],
        })
        migrate(str(self.dashboard_path), str(self.root / "schema"), "dashboard")
        reconcile_accounts(str(self.dashboard_path), str(self.proxy_path))
        with sqlite3.connect(self.dashboard_path) as conn:
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
                "INSERT INTO monthly_recurring_costs(month,account_id,"
                "billing_unit_id,recurring_charge,equivalent_cost,currency,"
                "normalized_recurring_cost) VALUES('2026-08',?,?,12,0,'CNY',12)",
                (self.plan_account_id, "unit-1"))
            # FX normalization pending: NULL normalized cost must surface as
            # billing_incomplete instead of counting as zero.
            conn.execute(
                "INSERT INTO monthly_recurring_costs(month,account_id,"
                "billing_unit_id,recurring_charge,equivalent_cost,currency,"
                "normalized_recurring_cost) VALUES('2026-08',?,?,9,0,'USD',NULL)",
                (self.plan_account_id, "unit-2"))
            conn.commit()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.app.config["DATA_STORE"].load()
        self.client = self.app.test_client()

    def test_summary_and_monthly_use_canonical_ledger(self) -> None:
        summary = self.client.get("/api/summary").get_json()
        # Legacy `total_cost` keeps the api-equivalent meaning (7.0 + 4.0);
        # the canonical V1 actual ledger is billed usage + recurring (5 + 12).
        self.assertAlmostEqual(summary["total_cost"], 11.0)
        self.assertAlmostEqual(summary["theoretical_cost"], 11.0)
        # actual = billed usage + normalized recurring (12), never + virtual.
        self.assertAlmostEqual(summary["actual_cost"], 17.0)
        self.assertEqual(summary["billing_incomplete_count"], 1)
        self.assertEqual(summary["billing_health"], "degraded")
        self.assertEqual(summary["plan_virtual_cost"], 0.0)

        monthly = self.client.get("/api/monthly").get_json()
        aug = next(m for m in monthly if m["year"] == 2026 and m["month"] == 8)
        # `cost` keeps the historical api-equivalent meaning (7+4), while the
        # metered bill plus recurring subscription is `actual_cost`.
        self.assertAlmostEqual(aug["cost"], 11.0)
        self.assertAlmostEqual(aug["theoretical_cost"], 11.0)
        self.assertAlmostEqual(aug["actual_cost"], 17.0)

    def test_daily_and_breakdown_keep_costs_separate(self) -> None:
        daily = self.client.get("/api/daily?year=2026&month=8").get_json()
        self.assertEqual(daily["year"], 2026)
        self.assertEqual(daily["month"], 8)
        flattened = daily["days"]
        self.assertTrue(flattened)
        costs = sum(row["cost"] for row in flattened)
        theoretical = sum(row["theoretical_cost"] for row in flattened)
        self.assertAlmostEqual(costs, 11.0)
        self.assertAlmostEqual(theoretical, 11.0)

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
