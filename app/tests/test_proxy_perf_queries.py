"""Performance read paths: proxy-only accounting and the time window.

The perf aggregates in ``app/db/proxy/performance.py`` describe proxy traffic.
Agent-imported rows are estimates, so they must not enter the observed
latency/speed/throughput statistics.  Their ``source_kind`` is ``'import'``,
while proxy rows stay ``'proxy'`` even when they belong to an agent account.

Window selection compares the stored ISO timestamps
(``%Y-%m-%dT%H:%M:%fZ`` for proxy rows, a second-precision ISO form for
imported rows) against a cutoff written in the same grammar, which orders the
comparison by time.  A cutoff whose separator differs from the stored
timestamps (``datetime('now', '-15 minutes')``) orders every timestamp on the
cutoff's UTC date as in-window, so the fixtures keep a 90-minute-old proxy row
and assert its exclusion.
"""

from __future__ import annotations

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from app.core.time import utc_now

from app.tests.support import AppDatabaseTestCase, sqlite_connection

WINDOW_MINUTES = 15
WINDOW_SECONDS = WINDOW_MINUTES * 60
RECENT_LAG = timedelta(minutes=10)
STALE_LAG = timedelta(minutes=90)


class ProxyPerfQueriesTest(AppDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        now = utc_now()
        recent = now - RECENT_LAG
        stale = now - STALE_LAG
        self.proxy_recent_at = recent.strftime("%Y-%m-%dT%H:%M:%S.250Z")
        self.proxy_stale_at = stale.strftime("%Y-%m-%dT%H:%M:%S.250Z")
        self.import_recent_at = recent.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.expected_bucket = recent.strftime("%Y-%m-%dT%H:%M:00Z")

        # Inside the window: one observed success and one observed error.
        self._insert_request(
            "perf-proxy-a", "proxy", "model-a", self.proxy_recent_at,
            status_code=200, prompt_tokens=100, completion_tokens=101,
            ttft_ms=800, duration_ms=2500, generation_ms=2000,
            output_tps=50.0, attempt_count=1)
        self._insert_request(
            "perf-proxy-b", "proxy", "model-b", self.proxy_recent_at,
            status_code=500, prompt_tokens=40, completion_tokens=0,
            duration_ms=120, attempt_count=1)
        # Older than the window, same timestamp grammar a proxy would write.
        self._insert_request(
            "perf-proxy-stale", "proxy", "model-a", self.proxy_stale_at,
            status_code=200, prompt_tokens=10, completion_tokens=11,
            ttft_ms=100, duration_ms=1100, generation_ms=1000,
            output_tps=9.0, attempt_count=1)
        # Imported rows: estimated metrics, no attempt bookkeeping.  The
        # exaggerated values make any leakage into the proxy statistics
        # obvious; the zero-valued row mirrors the shape importers write.
        self._insert_request(
            "perf-import-recent", "import", "model-a", self.import_recent_at,
            status_code=200, prompt_tokens=200, completion_tokens=201,
            ttft_ms=99999, duration_ms=1200, generation_ms=2000,
            output_tps=999.0)
        self._insert_request(
            "perf-import-only", "import", "import-only-model",
            self.import_recent_at, status_code=200, prompt_tokens=1,
            completion_tokens=2, ttft_ms=1000, duration_ms=1000,
            generation_ms=1000, output_tps=2.0)
        self._insert_request(
            "perf-import-zero", "import", "import-only-model",
            self.import_recent_at, status_code=200, prompt_tokens=3,
            completion_tokens=0, duration_ms=1500)

    def _insert_request(self, event_id: str, source_kind: str, model: str,
                        requested_at: str, *, status_code: int = 200,
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        ttft_ms: int = 0, duration_ms: int = 0,
                        generation_ms: int = 0, output_tps: float = 0.0,
                        attempt_count: int = 0) -> None:
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,model,"
                "requested_at,status_code,prompt_tokens,completion_tokens,"
                "total_tokens,duration_ms,ttft_ms,generation_ms,output_tps,"
                "attempt_count,pricing_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'frozen')",
                (event_id, source_kind, model, requested_at, status_code,
                 prompt_tokens, completion_tokens,
                 prompt_tokens + completion_tokens, duration_ms, ttft_ms,
                 generation_ms, output_tps, attempt_count),
            )

    def test_summary_counts_only_proxy_rows_in_window(self) -> None:
        summary = self.proxy_database().get_perf_summary(WINDOW_MINUTES)
        self.assertEqual(summary["total_requests"], 2)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["success_rate"], 50.0)
        # 201 + 40 proxy tokens; the imported fixtures carry their own totals.
        self.assertEqual(summary["total_tokens"], 241)
        self.assertEqual(summary["avg_ttft_ms"], 800.0)

    def test_window_excludes_rows_older_than_the_window(self) -> None:
        db = self.proxy_database()
        buckets = db.get_perf_throughput(WINDOW_MINUTES)
        self.assertEqual(
            [(row["bucket"], row["requests"]) for row in buckets],
            [(self.expected_bucket, 2)])
        # The stale proxy row is 90 minutes old and carries the proxy ISO
        # grammar: a cutoff whose separator differs from the stored timestamps
        # orders it as in-window (its UTC date equals the cutoff's date for
        # most of a UTC day) and raises this count to 3.
        self.assertEqual(
            db.get_perf_summary(WINDOW_MINUTES)["total_requests"], 2)

    def test_latency_and_speed_cover_only_proxy_observations(self) -> None:
        db = self.proxy_database()
        self.assertEqual(
            db.get_perf_latency(WINDOW_MINUTES),
            [{"bucket": self.expected_bucket, "p50": 800, "p95": 800,
              "p99": 800, "count": 1}])
        self.assertEqual(
            db.get_perf_speed(WINDOW_MINUTES),
            [{"bucket": self.expected_bucket, "p50": 50.0, "p95": 50.0,
              "p99": 50.0, "count": 1}])

    def test_models_ignore_imported_rows(self) -> None:
        per_model = {
            row["model"]: row
            for row in self.proxy_database().get_perf_models(WINDOW_MINUTES)
        }
        self.assertEqual(set(per_model), {"model-a", "model-b"})
        self.assertEqual(per_model["model-a"], {
            "model": "model-a", "requests": 1, "avg_ttft_ms": 800.0,
            "max_ttft_ms": 800, "ttft_samples": 1, "avg_output_tps": 50.0,
            "speed_samples": 1, "success_rate": 100.0,
        })
        self.assertEqual(per_model["model-b"], {
            "model": "model-b", "requests": 1, "avg_ttft_ms": None,
            "max_ttft_ms": None, "ttft_samples": 0, "avg_output_tps": None,
            "speed_samples": 0, "success_rate": 0.0,
        })

    def test_upstream_success_rate_uses_window_and_skips_imports(self) -> None:
        rows = self.proxy_database().get_perf_upstream_success_rate(
            WINDOW_MINUTES)
        self.assertEqual(rows, [{
            "account_name": "unknown", "total": 2, "errors": 1,
            "success_rate": 50.0,
        }])

    def test_realtime_rpm_counts_only_proxy_rows(self) -> None:
        db = self.proxy_database()
        with patch.dict(
                os.environ,
                {"TOKEN_PROXY_HEALTH_URL": "http://127.0.0.1:1/health"}):
            realtime = db.get_perf_realtime(window_seconds=WINDOW_SECONDS)
        self.assertEqual(realtime["recent_requests"], 2)
        self.assertEqual(realtime["rpm"], 0.1)


if __name__ == "__main__":
    unittest.main()
