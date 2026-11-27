"""ProxyDatabase methods for ProxyBillingLedgerMixin."""

from app.core.time import format_utc, utc_now
from app.db.proxy.common import timedelta
from app.services.billing_report import attributed_request_sql


class ProxyBillingLedgerMixin:
    def get_recent_billing_days(self, days: int = 30) -> list[str]:
        """Distinct attributed request dates in the last *days* days."""
        conn = self._connect()
        try:
            now = utc_now()
            rows = conn.execute("""
                SELECT DISTINCT date(r.requested_at) AS d
                FROM request_log r
            """ + attributed_request_sql("r") + """
                ORDER BY d
            """, {
                "start": format_utc(now - timedelta(days=int(days))),
                "end": format_utc(now),
            }).fetchall()
            return [r["d"] for r in rows]
        finally:
            conn.close()

    def get_today_upstream_usage(self) -> list[dict]:
        """Per attributed proxy account or Agent software used today."""
        conn = self._connect()
        try:
            now = utc_now()
            today = now.strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT ai.name AS account_name,
                       CASE WHEN ai.account_kind='agent' THEN 'agent'
                            ELSE r.source_kind END AS source_kind,
                       COALESCE(SUM(r.billed_usage_cost), 0) AS real_cost,
                       COALESCE(SUM(r.equivalent_cost), 0) AS api_equivalent_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
            """ + attributed_request_sql("r") + """ AND date(r.requested_at)=:today
                GROUP BY ai.id
                ORDER BY api_equivalent_cost DESC
            """, {"start": f"{today}T00:00:00Z", "end": format_utc(now),
                  "today": today}).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
