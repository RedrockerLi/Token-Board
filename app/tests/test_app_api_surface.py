from __future__ import annotations

from app import create_app
from app.tests.support import AppDatabaseTestCase


class AppApiSurfaceTest(AppDatabaseTestCase):
    """Smoke every read-only App endpoint against the official V1 fixture."""

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()

    def test_proxy_and_dashboard_reads_keep_public_contract(self) -> None:
        paths = [
            "/api/proxy/stats", "/api/proxy/account-types",
            "/api/proxy/accounts", "/api/proxy/keys",
            "/api/proxy/aggregates", "/api/proxy/pricing",
            "/api/proxy/timeout-config", "/api/proxy/billing-config",
            "/api/proxy/billing", "/api/proxy/billing/daily",
            "/api/proxy/billing/daily-by-model",
            "/api/proxy/billing/recent-days",
            "/api/proxy/billing/today-upstreams",
            "/api/proxy/logs", "/api/proxy/perf/summary",
            "/api/proxy/perf/upstream-success-rate",
            "/api/proxy/perf/latency", "/api/proxy/perf/speed",
            "/api/proxy/perf/throughput", "/api/proxy/perf/models",
            "/api/proxy/perf/realtime", "/api/proxy/sync/config",
            "/api/summary", "/api/monthly", "/api/daily?year=2026&month=8",
            "/api/model_breakdown", "/api/token_types_by_month?year=2026&month=8",
            "/api/api_key_names", "/api/models", "/api/token_types",
        ]
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200,
                             f"{path}: {response.get_data(as_text=True)}")
            self.assertIsNotNone(response.get_json(), path)

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
