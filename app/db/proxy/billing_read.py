"""ProxyDatabase methods for ProxyBillingReadMixin."""

from app.core.time import format_utc, utc_now
from app.db.proxy.common import datetime, timedelta
from app.services.billing_report import (
    _agent_allocation_sql,
    _proxy_charge_sql,
    live_request_sql,
)


def _filter_bound(value: str | None, *, end: bool = False) -> str | None:
    """Normalize a from/to filter to an ISO UTC timestamp.

    The UI sends ISO UTC timestamps; a bare "YYYY-MM-DD" (legacy callers) is
    expanded to the UTC day boundary so direct ``requested_at`` comparisons
    stay correct.
    """
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text + ("T23:59:59.999Z" if end else "T00:00:00Z")
    return text


class ProxyBillingReadMixin:
    @staticmethod
    def _utc_window(days: int) -> tuple[str, str]:
        now = utc_now()
        return (format_utc(now - timedelta(days=int(days))),
                format_utc(now))

    def get_plan_billing_config(self) -> dict:
        conn = self._connect()
        try:
            row = self._billing_config_conn(conn)
            return {
                "cancellation_mode": row["cancellation_mode"],
                "timezone": "UTC",
            }
        finally:
            conn.close()

    def update_plan_billing_config(self, data: dict) -> bool:
        if "price_change_effective" in data:
            raise ValueError("价格修改统一从下一计费周期生效")
        cancellation = data.get("cancellation_mode")
        if cancellation not in ("immediate", "end_of_period"):
            raise ValueError("删除默认操作必须是 immediate 或 end_of_period")
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO sync_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [("billing.cancellation_mode", cancellation)],
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def get_billing_summary(
        self,
        account_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        days: int = 30,
    ) -> list[dict]:
        """Aggregated current-state billing by account + model + date.

        Frozen request rows remain available for audit, but detached rows do
        not belong to the current consumption view.  The shared live request
        predicate is therefore applied before grouping.
        """
        conn = self._connect()
        try:
            now = utc_now()
            start = (_filter_bound(date_from) or
                     format_utc(now - timedelta(days=int(days))))
            end = _filter_bound(date_to, end=True) or format_utc(now)
            sql = """
                SELECT
                    live_account.name AS account_name,
                    live_account.id AS account_id,
                    r.agent_software_id,
                    r.model,
                    date(r.requested_at) AS date,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost
                FROM request_log r
            """ + live_request_sql("r")
            params = {"start": start, "end": end, "now": end}
            if account_id:
                sql += " AND live_account.id=:account_id"
                params["account_id"] = account_id

            sql += """
                GROUP BY live_account.id, r.model, date(r.requested_at)
                ORDER BY date(r.requested_at) DESC, live_account.id, r.model
            """
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_request_logs(
        self,
        page: int = 1,
        per_page: int = 50,
    ) -> dict:
        """Simple paginated request log without filter/query state."""
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 200))
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                """SELECT
                    r.id, COALESCE(r.account_identity_id,r.account_id) AS account_id, r.agent_software_id,
                    CASE WHEN COALESCE(ai.account_kind,a.account_kind)='agent' THEN 'agent'
                         ELSE r.source_kind END AS source_kind,
                    COALESCE(ai.name, a.name, 'unknown') AS account_name,
                    r.model, r.prompt_tokens, r.cache_read_tokens,
                    r.completion_tokens,
                    r.total_tokens, r.equivalent_cost AS cost,
                    r.billed_usage_cost, r.pricing_status,
                    r.queue_ms, r.accounting_ms, r.is_streaming,
                    r.status_code, r.ttft_ms, r.generation_ms, r.output_tps,
                    r.upstream_ttft_ms, r.upstream_duration_ms,
                    r.attempt_count, r.fallback_count,
                    r.requested_at
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                LEFT JOIN account_identities ai
                  ON ai.id=COALESCE(r.account_identity_id,r.account_id)
                ORDER BY r.requested_at DESC, r.id DESC
                LIMIT ? OFFSET ?""",
                (per_page, offset),
            ).fetchall()

            items = [dict(r) for r in rows]
            result = {
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
                "items": items,
            }
            conn.commit()
            return result
        finally:
            conn.close()

    def get_daily_billing(self, days: int = 30) -> list[dict]:
        """Daily metered and recurring cost, assigned to period-start dates."""
        conn = self._connect()
        try:
            start, end = self._utc_window(days)
            request_params = {"start": start, "end": end, "now": end}
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    live_account.id AS account_id,
                    live_account.name AS account_name,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS metered_cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost,
                    CASE WHEN live_account.account_kind='agent' THEN 'agent'
                         WHEN r.billing_unit_id IS NOT NULL THEN 'plan'
                         ELSE 'api' END AS account_type,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens
                FROM request_log r
            """ + live_request_sql("r") + """
                GROUP BY date(r.requested_at), live_account.id
                ORDER BY date, live_account.id
            """, request_params).fetchall()
            result = {}
            for row in rows:
                item = dict(row)
                item["recurring_cost"] = 0.0
                item["cost"] = float(item["metered_cost"] or 0)
                result[(item["date"], item["account_id"])] = item

            recurring_queries = (
                """SELECT date(c.period_start) date,live_account.id account_id,
                          live_account.name account_name,
                          COALESCE(SUM(c.normalized_recurring_cost),0) recurring_cost,
                          'plan' account_type
                """ + _proxy_charge_sql() + """
                   GROUP BY date(c.period_start),live_account.id""",
                """SELECT date(charge.period_start) date,live_software.id account_id,
                          live_agent_account.name account_name,
                          COALESCE(SUM(allocation.normalized_recurring_cost),0) recurring_cost,
                          'agent' account_type
                """ + _agent_allocation_sql() + """
                   GROUP BY date(charge.period_start),live_software.id""",
            )
            for sql in recurring_queries:
                for row in conn.execute(sql, request_params):
                    key = (row["date"], row["account_id"])
                    item = result.setdefault(key, {
                        "date": row["date"], "account_id": row["account_id"],
                        "account_name": row["account_name"],
                        "metered_cost": 0.0, "equivalent_cost": 0.0,
                        "account_type": row["account_type"],
                        "requests": 0, "total_tokens": 0,
                        "recurring_cost": 0.0, "cost": 0.0,
                    })
                    item["recurring_cost"] += float(row["recurring_cost"] or 0)
                    item["cost"] = (float(item.get("metered_cost") or 0) +
                                    float(item["recurring_cost"] or 0))
            return sorted(result.values(), key=lambda row: (row["date"], row["account_id"] or 0))
        finally:
            conn.close()

    def get_daily_billing_by_model(self, days: int = 30) -> list[dict]:
        """Daily traffic with separate actual and theoretical cost fields."""
        conn = self._connect()
        try:
            start, end = self._utc_window(days)
            params = {"start": start, "end": end, "now": end}
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(SUM(r.prompt_tokens), 0) AS input_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS output_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_hit_tokens,
                    MAX(COALESCE(SUM(r.prompt_tokens), 0) - COALESCE(SUM(r.cache_read_tokens), 0), 0) AS cache_miss_tokens,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS metered_cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost
                FROM request_log r
            """ + live_request_sql("r") + """
                GROUP BY date(r.requested_at)
                ORDER BY date
            """, params).fetchall()
            result = {row["date"]: dict(row) for row in rows}
            recurring_sql = (
                "SELECT date(c.period_start) date,"
                "COALESCE(SUM(c.normalized_recurring_cost),0) recurring_cost "
                + _proxy_charge_sql() + "GROUP BY date(c.period_start)",
                "SELECT date(charge.period_start) date,"
                "COALESCE(SUM(allocation.normalized_recurring_cost),0) recurring_cost "
                + _agent_allocation_sql() + "GROUP BY date(charge.period_start)",
            )
            for sql in recurring_sql:
                for row in conn.execute(sql, params):
                    item = result.setdefault(row["date"], {
                        "date": row["date"], "input_tokens": 0,
                        "output_tokens": 0, "cache_hit_tokens": 0,
                        "cache_miss_tokens": 0, "requests": 0,
                        "metered_cost": 0.0, "equivalent_cost": 0.0,
                    })
                    item["recurring_cost"] = (
                        float(item.get("recurring_cost") or 0) +
                        float(row["recurring_cost"] or 0))
            for item in result.values():
                item.setdefault("recurring_cost", 0.0)
                item["cost"] = (float(item.get("metered_cost") or 0) +
                                float(item["recurring_cost"] or 0))
            return [result[key] for key in sorted(result)]
        finally:
            conn.close()
