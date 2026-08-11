"""ProxyDatabase methods for ProxyPerformanceMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


class ProxyPerformanceMixin:
    def get_perf_summary(self, window_minutes: int = 15) -> dict:
        """Aggregated performance stats for the last N minutes.

        Data source: request_log, which records every request outcome
        (including aborted/timeout/error attempts). Errors = status_code >= 400.
        """
        conn = self._connect()
        try:
            total, successes, tokens, avg_ttft = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(total_tokens), 0), "
                "AVG(CASE WHEN status_code BETWEEN 200 AND 299 "
                "              AND ttft_ms IS NOT NULL THEN ttft_ms END) "
                "FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),),
            ).fetchone()
            successes = successes or 0
            errors = total - successes

            return {
                "total_requests": total,
                "error_count": errors,
                "success_rate": round(successes / max(total, 1) * 100, 1),
                "total_tokens": tokens,
                "avg_ttft_ms": round(avg_ttft, 1) if avg_ttft is not None else None,
            }
        finally:
            conn.close()

    def get_perf_latency(self, window_minutes: int = 60) -> list[dict]:
        """Observed streaming TTFT percentiles per bucket."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:00Z', requested_at) AS bucket, "
                "ttft_ms FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  AND status_code BETWEEN 200 AND 299 "
                "  AND ttft_ms IS NOT NULL "
                "ORDER BY bucket, ttft_ms",
                (str(window_minutes),),
            ).fetchall()
            result = []
            by_bucket = {}
            for bucket, ttft_ms in rows:
                by_bucket.setdefault(bucket, []).append(ttft_ms)
            for bucket, vals in by_bucket.items():
                if not vals:
                    continue
                n = len(vals)

                def percentile(p):
                    # Nearest-rank percentile: ceil(p*n/100)-1.
                    k = max(0, min(n - 1, (p * n + 99) // 100 - 1))
                    return vals[k]

                result.append({
                    "bucket": bucket,
                    "p50": percentile(50),
                    "p95": percentile(95),
                    "p99": percentile(99),
                    "count": n,
                })
            return result
        finally:
            conn.close()

    def get_perf_speed(self, window_minutes: int = 60) -> list[dict]:
        """Observed streaming output-speed (tokens/s) percentiles per bucket."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:00Z', requested_at) AS bucket, "
                "output_tps FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  AND status_code BETWEEN 200 AND 299 "
                "  AND output_tps IS NOT NULL "
                "ORDER BY bucket, output_tps",
                (str(window_minutes),),
            ).fetchall()
            result = []
            by_bucket = {}
            for bucket, tps in rows:
                by_bucket.setdefault(bucket, []).append(tps)
            for bucket, vals in by_bucket.items():
                if not vals:
                    continue
                n = len(vals)

                def percentile(p):
                    # Nearest-rank percentile: ceil(p*n/100)-1.
                    k = max(0, min(n - 1, (p * n + 99) // 100 - 1))
                    return vals[k]

                result.append({
                    "bucket": bucket,
                    "p50": round(percentile(50), 2),
                    "p95": round(percentile(95), 2),
                    "p99": round(percentile(99), 2),
                    "count": n,
                })
            return result
        finally:
            conn.close()

    def get_perf_throughput(self, window_minutes: int = 60) -> list[dict]:
        """Request count per 1-minute bucket (from request_log)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:00Z', requested_at) AS bucket, "
                "COUNT(*) AS request_count "
                "FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "GROUP BY bucket "
                "ORDER BY bucket",
                (str(window_minutes),)
            ).fetchall()

            return [{"bucket": r[0], "requests": r[1]} for r in rows]
        finally:
            conn.close()

    def get_perf_models(self, window_minutes: int = 60, max_samples: int = 100) -> list[dict]:
        """Per-model observed TTFT and weighted output speed.

        Samples only the *max_samples* most recent request_log rows per model
        (within the window), so high-traffic models don't dominate the
        averages; `ttft_samples`/`speed_samples` report how many of those rows
        actually carried a TTFT / speed observation.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "WITH ranked AS ("
                "  SELECT model, status_code, ttft_ms, generation_ms, completion_tokens, "
                "         ROW_NUMBER() OVER (PARTITION BY model "
                "                            ORDER BY requested_at DESC, id DESC) AS rn "
                "  FROM request_log "
                "  WHERE requested_at >= datetime('now', '-' || ? || ' minutes')"
                ") "
                "SELECT model, COUNT(*) AS request_count, "
                "AVG(CASE WHEN status_code BETWEEN 200 AND 299 THEN ttft_ms END) AS avg_ttft_ms, "
                "MAX(CASE WHEN status_code BETWEEN 200 AND 299 THEN ttft_ms END) AS max_ttft_ms, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND ttft_ms IS NOT NULL "
                "         THEN 1 ELSE 0 END) AS ttft_samples, "
                "CASE WHEN SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                              AND completion_tokens > 1 "
                "                   THEN generation_ms ELSE 0 END) > 0 "
                "THEN SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                   AND completion_tokens > 1 "
                "              THEN completion_tokens - 1 ELSE 0 END) * 1000.0 / "
                "     SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                   AND completion_tokens > 1 "
                "              THEN generation_ms ELSE 0 END) END AS avg_output_tps, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "         AND completion_tokens > 1 THEN 1 ELSE 0 END) AS speed_samples, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_count "
                "FROM ranked "
                "WHERE rn <= ? "
                "GROUP BY model "
                "ORDER BY request_count DESC",
                (str(window_minutes), max_samples),
            ).fetchall()

            return [{
                "model": r[0],
                "requests": r[1],
                # NULL means no semantic TTFT was observed (for example a
                # non-streaming request), not a zero-millisecond response.
                "avg_ttft_ms": round(r[2], 1) if r[2] is not None else None,
                "max_ttft_ms": r[3] if r[3] is not None else None,
                "ttft_samples": r[4] or 0,
                "avg_output_tps": round(r[5], 2) if r[5] is not None else None,
                "speed_samples": r[6] or 0,
                "success_rate": round(r[7] / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_upstream_success_rate(self, window_minutes: int = 60) -> list[dict]:
        """Per upstream success rate for the last N minutes."""
        conn = self._connect()
        try:
            observations = (
                "WITH observations(account_id, status_code, requested_at) AS ("
                "  SELECT account_id, status_code, requested_at FROM request_attempts "
                "  WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  UNION ALL "
                "  SELECT r.account_id, r.status_code, r.requested_at FROM request_log r "
                "  WHERE COALESCE(r.attempt_count, 1) > 0 "
                "    AND r.requested_at >= datetime('now', '-' || ? || ' minutes') "
                "    AND NOT EXISTS (SELECT 1 FROM request_attempts t "
                "                    WHERE t.request_log_id = r.id)"
                ") "
            )
            query = observations + "SELECT COALESCE(a.name, 'unknown') AS account_name, "
            query += (
                "COUNT(*) AS total, "
                "SUM(CASE WHEN o.status_code BETWEEN 200 AND 299 "
                "         THEN 1 ELSE 0 END) AS successes "
                "FROM observations o LEFT JOIN accounts a ON a.id=o.account_id "
                "WHERE o.status_code != 499 GROUP BY o.account_id ORDER BY total DESC"
            )
            rows = conn.execute(
                query,
                (str(window_minutes), str(window_minutes)),
            ).fetchall()

            return [{
                "account_name": r[0],
                "total": r[1],
                "errors": r[1] - (r[2] or 0),
                "success_rate": round((r[2] or 0) / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_realtime(self, window_seconds: int = 60) -> dict:
        """Real-time metrics: current RPM estimate and live concurrency.

        RPM is estimated from request_log.  Live concurrency comes from the
        proxy's process-local counter so request forwarding never writes an
        observability row before contacting the upstream.  An unreachable
        proxy is reported as unavailable rather than the misleading zero from
        the legacy table.
        """
        conn = self._connect()
        try:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' seconds')",
                (str(window_seconds),)
            ).fetchone()[0]

            rpm = round(recent_count / max(window_seconds / 60.0, 0.1), 1)
            unrated_usage = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE pricing_status='unrated'"
            ).fetchone()[0]
            billing_incomplete = conn.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE finalized_at IS NULL "
                "AND (normalized_recurring_cost IS NULL "
                "OR (currency!='CNY' AND fx_rate_date!=date(period_start)))"
            ).fetchone()[0]
            sync_row = conn.execute(
                "SELECT value FROM sync_state WHERE key='sync_health'"
            ).fetchone()
            sync_health = sync_row[0] if sync_row else "unconfigured"

            latest_concurrent = None
            health_fields = {
                "accounting": {}, "transport": {}, "queue": {},
                "schema": {}, "routing": {}, "recovery": {},
                "status": "unavailable",
            }
            proxy_port = os.environ.get("TOKEN_PROXY_PORT", "8800")
            health_url = os.environ.get(
                "TOKEN_PROXY_HEALTH_URL", f"http://127.0.0.1:{proxy_port}/health"
            )
            try:
                with urllib.request.urlopen(health_url, timeout=1.0) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if isinstance(health.get("concurrency"), int):
                    latest_concurrent = health["concurrency"]
                health_fields = {
                    "accounting": health.get("accounting", {}),
                    "transport": health.get("transport", {}),
                    "queue": health.get("queue", {}),
                    "schema": health.get("schema", {}),
                    "routing": health.get("routing", {}),
                    "recovery": health.get("recovery", {}),
                    "status": health.get("status"),
                }
            except (OSError, ValueError, json.JSONDecodeError):
                # The proxy may be stopped while the App remains available.
                latest_concurrent = None

            # The in_flight_requests table was dropped in migration 0017; live
            # concurrency comes from /health above.
            return {
                "rpm": rpm,
                "recent_requests": recent_count,
                "latest_concurrent": latest_concurrent,
                "in_flight": [],
                "unrated_usage": unrated_usage,
                "billing_incomplete": billing_incomplete,
                "billing_health": "degraded" if (unrated_usage or billing_incomplete)
                else "ok",
                # Sync health is persisted so a transient WebDAV failure stays
                # visible after the background thread or App process restarts.
                "sync_health": sync_health,
                **health_fields,
            }
        finally:
            conn.close()
