from __future__ import annotations

import sqlite3

from app import create_app

from app.tests.support import AppDatabaseTestCase, sqlite_connection


class DashboardReportsTest(AppDatabaseTestCase):
    """Golden-cost assertions for the dashboard report routes.

    These pin the V2.2 dashboard contract: actual cost is stored on stable
    users, while daily model rows provide token counts and theoretical cost.
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
        with sqlite_connection(self.dashboard_path) as conn:
            conn.executemany(
                "INSERT INTO users(id,name,actual_cost_micro_cny) VALUES(?,?,?)",
                [(self.api_account_id, "report-account", 5_000_000),
                 (self.plan_account_id, "report-plan", 12_000_000)],
            )
            conn.execute(
                "INSERT INTO daily_model_usage("
                "user_id,usage_date,model,input_tokens,cache_read_tokens,"
                "output_tokens,request_count,api_equivalent_cost_micro_cny) "
                "VALUES(?,?,?,40,0,10,2,7000000)",
                (self.api_account_id, "2026-08-09", "model-a"))
            conn.execute(
                "INSERT INTO daily_model_usage("
                "user_id,usage_date,model,input_tokens,cache_read_tokens,"
                "output_tokens,request_count,api_equivalent_cost_micro_cny) "
                "VALUES(?,?,?,10,0,20,1,4000000)",
                (self.api_account_id, "2026-08-10", "model-b"))
            conn.execute(
                "INSERT INTO daily_model_usage("
                "user_id,usage_date,model,input_tokens,cache_read_tokens,"
                "output_tokens,request_count,api_equivalent_cost_micro_cny) "
                "VALUES(?,?,?,5,0,5,1,9000000)",
                (self.plan_account_id, "2026-08-11", "plan-model"))
            conn.commit()

        # The dashboard archive is not the source of truth for current
        # actual cost.  Keep matching live Token-Board facts in the fixture:
        # the archive rows above alone must not make a deleted/live decision.
        with sqlite_connection(self.proxy_path) as conn:
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

    def test_summary_uses_canonical_ledger(self) -> None:
        summary = self.client.get("/api/summary").get_json()
        # V2.2 stores actual cost on the stable user identity and keeps model
        # costs theoretical in the daily projection.
        self.assertAlmostEqual(summary["actual_cost"], 17.0)
        self.assertAlmostEqual(summary["theoretical_total_cost"], 20.0)
        self.assertEqual(summary["total_tokens"], 90)
        self.assertEqual(summary["total_requests"], 4)
        self.assertAlmostEqual(
            summary["model_breakdown"]["model-a"]["theoretical_cost"], 7.0)
        self.assertAlmostEqual(
            summary["model_breakdown"]["model-b"]["theoretical_cost"], 4.0)
        self.assertAlmostEqual(
            summary["model_breakdown"]["plan-model"]["theoretical_cost"], 9.0)
        self.assertAlmostEqual(summary["model_breakdown"]["plan-model"]["cost"], 9.0)

    def test_daily_and_breakdown_keep_token_grains_separate(self) -> None:
        daily = self.client.get("/api/daily?year=2026&month=8").get_json()
        self.assertEqual(daily["year"], 2026)
        self.assertEqual(daily["month"], 8)
        days = daily["days"]
        self.assertEqual([row["date"] for row in days], [
            "2026-08-09", "2026-08-10", "2026-08-11",
        ])
        self.assertEqual(sum(row["total_tokens"] for row in days), 90)
        self.assertEqual(sum(row["requests"] for row in days), 4)

    def test_daily_model_filter_uses_virtual_cost_not_subscription(self) -> None:
        model_a = self.client.get(
            "/api/daily?year=2026&month=8&model=model-a").get_json()
        self.assertEqual(len(model_a["days"]), 1)
        row = model_a["days"][0]
        self.assertEqual(row["date"], "2026-08-09")
        self.assertEqual(row["input_tokens"], 40)
        self.assertEqual(row["output_tokens"], 10)
        self.assertEqual(row["total_tokens"], 50)
        self.assertEqual(row["requests"], 2)

        plan_model = self.client.get(
            "/api/daily?year=2026&month=8&model=plan-model").get_json()
        self.assertEqual(len(plan_model["days"]), 1)
        row = plan_model["days"][0]
        self.assertEqual(row["date"], "2026-08-11")
        self.assertEqual(row["input_tokens"], 5)
        self.assertEqual(row["output_tokens"], 5)
        self.assertEqual(row["total_tokens"], 10)
        self.assertEqual(row["requests"], 1)

    def test_frozen_dashboard_cost_does_not_change_after_live_delete(self) -> None:
        before = self.client.get("/api/summary").get_json()

        database = self.proxy_database()
        database.update_plan_billing_config({"cancellation_mode": "immediate"})
        self.assertTrue(database.delete_account(
            self.plan_account_id, mode="immediate")["ok"])

        # The live plan graph is gone, but Dashboard's already exported
        # frozen amount remains the same immutable historical fact.
        self.app.config["DATA_STORE"].load()
        after = self.client.get("/api/summary").get_json()
        self.assertAlmostEqual(
            after["theoretical_total_cost"], before["theoretical_total_cost"])
        self.assertAlmostEqual(after["actual_cost"], before["actual_cost"])

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
