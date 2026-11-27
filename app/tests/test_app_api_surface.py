from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class AppApiSurfaceTest(AppDatabaseTestCase):
    """Smoke every read-only App endpoint against the current V2 schema.

    The fixture lives in a real V2 database pair created by
    ``ensure_local_databases``; there is no V1 fixture involved.
    """

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
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,source_kind,account_id,model,prompt_tokens,"
                "completion_tokens,cache_read_tokens,total_tokens,equivalent_cost,"
                "billed_usage_cost,status_code,attempt_count,requested_at,pricing_status,"
                "is_streaming,queue_ms,accounting_ms,duration_ms,ttft_ms,"
                "generation_ms,output_tps,upstream_ttft_ms,upstream_duration_ms)"
                "VALUES('api-surface-request','proxy',?,'surface-model',100,"
                "20,10,120,0.001,0.001,200,1,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'frozen',"
                "1,3,4,1250,250,1000,42.5,200,1100)",
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
            "total_cost", "today_cost", "api_equivalent_cost", "today_actual_cost",
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
                "short_label", "subtypes",
            } <= payload.keys())

        timeout = self.client.get("/api/proxy/timeout-config").get_json()
        self.assertEqual(set(timeout),
                         {"anthropic", "openai", "openai_responses", "google"})
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
            "auth_header", "is_aggregate", "account_type", "subtype",
            "monthly_price", "currency", "valid_from", "max_concurrency",
            "created_at", "ends_at", "key_count", "keys", "cloud_keys",
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
            "weighted_total_tokens", "total_requests", "actual_cost",
            "api_equivalent_total_cost",
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

    # ---- TB05-F06 / TB05-F11: content, not only shape -------------------

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _schema_tip_minor(self) -> int:
        versions = []
        for path in (self.root / "schema" / "token-board" / "v2").glob("*.sql"):
            versions.append(int(path.name.split("-", 1)[1].split("_", 1)[0]))
        return max(versions)

    def test_seeded_read_endpoints_return_content(self) -> None:
        """The 11 shape-only list endpoints must expose the seeded values."""
        today = self._today()

        billing = self.client.get("/api/proxy/billing").get_json()
        self.assertEqual(len(billing), 1)
        row = billing[0]
        self.assertEqual(
            (row["account_name"], row["account_id"], row["model"], row["date"],
             row["requests"], row["prompt_tokens"], row["completion_tokens"],
             row["total_tokens"]),
            ("api-surface-account", self.account_id, "surface-model", today,
             1, 100, 20, 120))
        self.assertAlmostEqual(row["cost"], 0.001)
        self.assertAlmostEqual(row["equivalent_cost"], 0.001)

        daily = self.client.get("/api/proxy/billing/daily").get_json()
        self.assertEqual(len(daily), 1)
        row = daily[0]
        self.assertEqual(
            (row["date"], row["account_id"], row["account_name"],
             row["account_type"], row["requests"], row["total_tokens"]),
            (today, self.account_id, "api-surface-account", "api", 1, 120))
        self.assertAlmostEqual(row["metered_cost"], 0.001)
        self.assertAlmostEqual(row["equivalent_cost"], 0.001)
        self.assertAlmostEqual(row["recurring_cost"], 0.0)
        self.assertAlmostEqual(row["cost"], 0.001)

        by_model = self.client.get("/api/proxy/billing/daily-by-model").get_json()
        self.assertEqual(len(by_model), 1)
        row = by_model[0]
        self.assertEqual(
            (row["date"], row["input_tokens"], row["output_tokens"],
             row["cache_hit_tokens"], row["cache_miss_tokens"],
             row["requests"]),
            (today, 100, 20, 10, 90, 1))
        self.assertAlmostEqual(row["metered_cost"], 0.001)
        self.assertAlmostEqual(row["equivalent_cost"], 0.001)
        self.assertAlmostEqual(row["cost"], 0.001)

        recent = self.client.get("/api/proxy/billing/recent-days").get_json()
        self.assertEqual(recent, [today])

        upstreams = self.client.get("/api/proxy/billing/today-upstreams").get_json()
        self.assertEqual(len(upstreams), 1)
        row = upstreams[0]
        self.assertEqual(
            (row["account_name"], row["source_kind"], row["requests"],
             row["tokens"]),
            ("api-surface-account", "proxy", 1, 120))
        self.assertAlmostEqual(row["real_cost"], 0.001)
        self.assertAlmostEqual(row["api_equivalent_cost"], 0.001)

        success = self.client.get(
            "/api/proxy/perf/upstream-success-rate").get_json()
        self.assertEqual(success, [{
            "account_name": "api-surface-account", "total": 1,
            "errors": 0, "success_rate": 100.0,
        }])

        latency = self.client.get("/api/proxy/perf/latency").get_json()
        self.assertEqual(len(latency), 1)
        self.assertEqual((latency[0]["count"], latency[0]["p50"],
                          latency[0]["p95"], latency[0]["p99"]),
                         (1, 250, 250, 250))
        self.assertTrue(latency[0]["bucket"].startswith(today))

        speed = self.client.get("/api/proxy/perf/speed").get_json()
        self.assertEqual(len(speed), 1)
        self.assertEqual((speed[0]["count"], speed[0]["p50"]), (1, 42.5))
        self.assertTrue(speed[0]["bucket"].startswith(today))

        throughput = self.client.get("/api/proxy/perf/throughput").get_json()
        self.assertEqual(sum(item["requests"] for item in throughput), 1)
        self.assertTrue(throughput[0]["bucket"].startswith(today))

        models = self.client.get("/api/proxy/perf/models").get_json()
        self.assertEqual(len(models), 1)
        self.assertEqual(
            (models[0]["model"], models[0]["requests"],
             models[0]["avg_ttft_ms"], models[0]["max_ttft_ms"],
             models[0]["ttft_samples"], models[0]["avg_output_tps"],
             models[0]["speed_samples"], models[0]["success_rate"]),
            ("surface-model", 1, 250.0, 250, 1, 19.0, 1, 100.0))

        summary = self.client.get("/api/proxy/perf/summary").get_json()
        self.assertEqual(
            (summary["total_requests"], summary["error_count"],
             summary["success_rate"], summary["total_tokens"],
             summary["avg_ttft_ms"]),
            (1, 0, 100.0, 120, 250.0))

        sync_config = self.client.get("/api/proxy/sync/config").get_json()
        self.assertEqual(sync_config, {
            "base_url": "", "folder": "token-board-sync", "username": "",
            "password": "", "has_password": False,
        })

        created = self.client.post("/api/proxy/aggregates", json={
            "name": "surface-aggregate",
            "entries": [{"pattern": "surface-model",
                         "account_id": self.account_id,
                         "upstream_model": "surface-model"}],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        aggregate_id = created.get_json()["id"]
        aggregates = self.client.get("/api/proxy/aggregates").get_json()
        self.assertEqual(len(aggregates), 1)
        self.assertEqual((aggregates[0]["id"], aggregates[0]["name"]),
                         (aggregate_id, "surface-aggregate"))
        self.assertEqual(len(aggregates[0]["entries"]), 1)
        self.assertEqual(
            (aggregates[0]["entries"][0]["pattern"],
             aggregates[0]["entries"][0]["upstream_model"]),
            ("surface-model", "surface-model"))

    def test_v2_billing_semantics_are_numeric(self) -> None:
        """TB05-F11: V2 keeps API-equivalent and actual cost separate."""
        stats = self.client.get("/api/proxy/stats").get_json()
        self.assertEqual((stats["total_requests"], stats["today_requests"],
                          stats["total_tokens"]), (1, 1, 120))
        self.assertAlmostEqual(stats["api_equivalent_cost"], 0.001)
        self.assertAlmostEqual(stats["today_actual_cost"], 0.001)
        self.assertAlmostEqual(stats["today_cost"], 0.001)
        self.assertEqual(stats["billing_incomplete_count"], 0)
        self.assertEqual(stats["billing_health"], "ok")
        self.assertEqual(stats["active_upstreams"], 1)
        self.assertEqual(stats["active_accounts"], 1)
        # total_cost is metered + recurring, both exposed separately.
        self.assertAlmostEqual(stats["total_cost"],
                               round(stats["metered_cost"]
                                     + stats["recurring_cost"], 4))
        self.assertIsInstance(stats["metered_cost"], float)

        logs = self.client.get("/api/proxy/logs").get_json()["items"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["pricing_status"], "frozen")
        self.assertEqual(
            (logs[0]["model"], logs[0]["prompt_tokens"],
             logs[0]["completion_tokens"], logs[0]["total_tokens"],
             logs[0]["cache_read_tokens"]),
            ("surface-model", 100, 20, 120, 10))
        self.assertAlmostEqual(logs[0]["cost"], 0.001)
        self.assertAlmostEqual(logs[0]["billed_usage_cost"], 0.001)

    def test_realtime_reports_unavailable_runtime_and_billing_health(self) -> None:
        # A dev machine may already run the proxy on the default port; hide
        # the health endpoint so the "proxy stopped" contract is exercised.
        with patch("app.db.proxy.performance.urllib.request.urlopen",
                   side_effect=OSError("proxy is not listening")):
            payload = self.client.get("/api/proxy/perf/realtime").get_json()
        # No proxy process runs in the App test bench: health fields are empty
        # and the status is an explicit "unavailable", not a fake zero.
        self.assertEqual(payload["status"], "unavailable")
        for field in ("accounting", "transport", "queue", "schema",
                      "routing", "recovery"):
            self.assertEqual(payload[field], {}, field)
        self.assertIsNone(payload["latest_concurrent"])
        self.assertEqual(payload["in_flight"], [])
        self.assertEqual(payload["recent_requests"], 1)
        self.assertAlmostEqual(payload["rpm"], 1.0)
        self.assertEqual(payload["unrated_usage"], 0)
        self.assertEqual(payload["billing_incomplete"], 0)
        self.assertEqual(payload["billing_health"], "ok")
        self.assertEqual(payload["sync_health"], "unconfigured")

    def test_realtime_passes_through_proxy_health(self) -> None:
        tip_minor = self._schema_tip_minor()

        class HealthResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "concurrency": 3,
                    "accounting": {"pending": 2},
                    "transport": {"open": 1},
                    "queue": {"waiting": 0},
                    "schema": {"major": 2, "minor": tip_minor},
                    "routing": {"candidates": 4},
                    "recovery": {"status": "clean"},
                    "status": "ok",
                }).encode("utf-8")

        with patch("app.db.proxy.performance.urllib.request.urlopen",
                   return_value=HealthResponse()):
            payload = self.client.get("/api/proxy/perf/realtime").get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["latest_concurrent"], 3)
        self.assertEqual(payload["accounting"], {"pending": 2})
        self.assertEqual(payload["transport"], {"open": 1})
        self.assertEqual(payload["queue"], {"waiting": 0})
        self.assertEqual(payload["schema"], {"major": 2, "minor": tip_minor})
        self.assertEqual(payload["routing"], {"candidates": 4})
        self.assertEqual(payload["recovery"], {"status": "clean"})

    def test_dashboard_daily_reports_exported_tokens(self) -> None:
        from app.services.sync.dashboard_sync import sync_dashboard

        result = sync_dashboard(str(self.proxy_path), str(self.dashboard_path),
                                schema_dir=str(self.root / "schema"))
        self.assertEqual(result["status"], "ok", result)
        self.app.config["DATA_STORE"].load()

        now = datetime.now(timezone.utc)
        payload = self.client.get(
            f"/api/daily?year={now.year}&month={now.month}").get_json()
        self.assertEqual((payload["year"], payload["month"]),
                         (now.year, now.month))
        self.assertEqual(len(payload["days"]), 1)
        day = payload["days"][0]
        self.assertEqual(day["date"], now.strftime("%Y-%m-%d"))
        self.assertEqual(
            (day["output_tokens"], day["input_cache_hit_tokens"],
             day["input_cache_miss_tokens"], day["input_tokens"],
             day["total_tokens"], day["requests"]),
            (20, 10, 90, 100, 120, 1))
        self.assertEqual(day["by_model"]["surface-model"], {
            "output_tokens": 20,
            "input_cache_hit_tokens": 10,
            "input_cache_miss_tokens": 90,
            "total_tokens": 120,
            "requests": 1,
        })

        token_types = self.client.get("/api/token_types").get_json()
        self.assertEqual({item["name"]: item["value"] for item in token_types},
                         {"输出Token": 20, "输入缓存命中": 10,
                          "输入缓存未命中": 90})

        summary = self.client.get("/api/summary").get_json()
        self.assertEqual(
            (summary["total_output_tokens"],
             summary["total_input_cache_hit_tokens"],
             summary["total_input_cache_miss_tokens"],
             summary["total_input_tokens"], summary["total_tokens"],
             summary["total_requests"]),
            (20, 10, 90, 100, 120, 1))
        self.assertAlmostEqual(summary["api_equivalent_total_cost"], 0.001)
        self.assertAlmostEqual(summary["actual_cost"], 0.001)
        self.assertEqual(summary["models"], ["surface-model"])
        self.assertEqual(sorted(user["id"] for user in summary["users"]),
                         [0, self.account_id])
        self.assertIn("api-surface-account",
                      {user["name"] for user in summary["users"]})
        self.assertEqual(summary["available_months"][0]["year"], now.year)
