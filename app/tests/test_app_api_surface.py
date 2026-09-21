from __future__ import annotations

import sqlite3

from app import create_app
from app.tests.support import AppDatabaseTestCase


class AppApiSurfaceTest(AppDatabaseTestCase):
    """Smoke every read-only App endpoint against the official V1 fixture."""

    def setUp(self) -> None:
        super().setUp()
        database = self.proxy_database()
        self.account_id = database.create_account({
            "name": "api-surface-account",
            "account_type": "api",
            "base_url": "http://surface-upstream.test",
            "upstream_keys": ["sk-api-surface"],
        })
        database.create_key({"account_id": self.account_id,
                             "label": "surface-client"})
        database.create_pricing({
            "model_pattern": "surface-model",
            "input_price": 1,
            "cache_read_price": 0.5,
            "output_price": 2,
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,source_kind,account_id,model,prompt_tokens,"
                "completion_tokens,cache_read_tokens,total_tokens,equivalent_cost,"
                "billed_usage_cost,status_code,attempt_count,requested_at,pricing_status)"
                "VALUES('api-surface-request','proxy',?,'surface-model',100,"
                "20,10,120,0.001,0.001,200,1,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'frozen')",
                (self.account_id,),
            )
            conn.commit()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()

    def test_proxy_and_dashboard_reads_keep_public_contract(self) -> None:
        list_paths = (
            "/api/proxy/accounts", "/api/proxy/keys",
            "/api/proxy/aggregates", "/api/proxy/pricing",
            "/api/proxy/billing", "/api/proxy/billing/daily",
            "/api/proxy/billing/daily-by-model",
            "/api/proxy/billing/recent-days",
            "/api/proxy/billing/today-upstreams",
            "/api/proxy/perf/upstream-success-rate",
            "/api/proxy/perf/latency", "/api/proxy/perf/speed",
            "/api/proxy/perf/throughput", "/api/proxy/perf/models",
        )
        for path in list_paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200,
                             f"{path}: {response.get_data(as_text=True)}")
            self.assertIsInstance(response.get_json(), list, path)

        object_paths = (
            "/api/proxy/stats", "/api/proxy/account-types",
            "/api/proxy/timeout-config", "/api/proxy/billing-config",
            "/api/proxy/perf/summary", "/api/proxy/perf/realtime",
            "/api/proxy/sync/config",
        )
        for path in object_paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200,
                             f"{path}: {response.get_data(as_text=True)}")
            self.assertIsInstance(response.get_json(), dict, path)

        stats = self.client.get("/api/proxy/stats").get_json()
        self.assertTrue({
            "total_requests", "today_requests", "metered_cost", "recurring_cost",
            "total_cost", "today_cost", "theoretical_cost", "today_actual_cost",
            "total_tokens", "active_upstreams", "active_accounts",
            "billing_incomplete_count", "billing_health",
        } <= stats.keys())
        self.assertIsInstance(stats["total_requests"], int)

        account_types = self.client.get("/api/proxy/account-types").get_json()
        self.assertEqual(set(account_types), {"api", "plan"})
        for payload in account_types.values():
            self.assertTrue({
                "billing", "routable", "holds_keys", "usage_source",
                "deletion", "cooldown", "subscription_unit", "label",
                "short_label",
            } <= payload.keys())

        timeout = self.client.get("/api/proxy/timeout-config").get_json()
        self.assertEqual(set(timeout), {"anthropic", "openai", "openai_responses"})
        for group in timeout.values():
            self.assertTrue({
                "app_type", "streaming_first_byte_timeout",
                "streaming_idle_timeout", "non_streaming_timeout",
            } <= group.keys())

        billing_config = self.client.get("/api/proxy/billing-config").get_json()
        self.assertEqual(set(billing_config), {"cancellation_mode", "timezone"})

        accounts = self.client.get("/api/proxy/accounts").get_json()
        self.assertEqual(len(accounts), 1)
        self.assertTrue({
            "id", "name", "base_url", "api_format", "endpoint_path",
            "auth_header", "is_aggregate", "account_type", "monthly_price",
            "currency", "valid_from", "max_concurrency", "created_at",
            "ends_at", "key_count", "keys", "cloud_keys",
        } <= accounts[0].keys())
        keys = self.client.get("/api/proxy/keys").get_json()
        self.assertEqual(len(keys), 1)
        self.assertTrue({
            "id", "key_value", "label", "account_id", "account_name",
            "account_format", "created_at", "last_used_at",
        } <= keys[0].keys())
        pricing = self.client.get("/api/proxy/pricing").get_json()
        self.assertEqual(len(pricing), 1)
        self.assertTrue({
            "id", "model_pattern", "input_price", "cache_read_price",
            "output_price", "currency", "slots", "length_tiers",
        } <= pricing[0].keys())

        paged = self.client.get("/api/proxy/logs").get_json()
        self.assertTrue({"total", "page", "per_page", "total_pages", "items"}
                        <= paged.keys())
        self.assertEqual(len(paged["items"]), 1)
        self.assertTrue({
            "id", "account_id", "agent_software_id", "source_kind",
            "account_name", "model", "prompt_tokens", "cache_read_tokens",
            "completion_tokens", "total_tokens", "cost", "billed_usage_cost",
            "pricing_status", "status_code", "requested_at",
        } <= paged["items"][0].keys())

        realtime = self.client.get("/api/proxy/perf/realtime").get_json()
        self.assertTrue({
            "background_tasks", "background_health", "billing_health",
            "accounting", "transport", "queue", "schema", "routing",
            "recovery", "status",
        } <= realtime.keys())

        summary = self.client.get("/api/summary").get_json()
        self.assertEqual(set(summary), {
            "total_output_tokens", "total_input_cache_hit_tokens",
            "total_input_cache_miss_tokens", "total_input_tokens", "total_tokens",
            "total_requests", "actual_cost", "theoretical_total_cost",
            "model_breakdown", "users", "models", "available_months",
        })
        daily = self.client.get("/api/daily?year=2026&month=8").get_json()
        self.assertEqual(set(daily), {"year", "month", "days"})
        self.assertIsInstance(daily["days"], list)
        token_types = self.client.get("/api/token_types").get_json()
        self.assertEqual(len(token_types), 3)
        self.assertTrue(all(set(item) == {"name", "value"}
                            for item in token_types))

    def test_removed_dashboard_reports_are_not_registered(self) -> None:
        for path in (
            "/api/monthly",
            "/api/model_breakdown",
            "/api/token_types_by_month?year=2026&month=8",
            "/api/api_key_names",
            "/api/models",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_realtime_exposes_runtime_health_without_proxy_process(self) -> None:
        response = self.client.get("/api/proxy/perf/realtime")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("background_tasks", payload)
        self.assertIn("billing_health", payload)
        # The App remains useful while the C++ process is stopped, but keeps
        # the same transport/queue/recovery shape for dashboard consumers.
        for field in ("accounting", "transport", "queue", "schema",
                      "routing", "recovery", "status"):
            self.assertIn(field, payload)
