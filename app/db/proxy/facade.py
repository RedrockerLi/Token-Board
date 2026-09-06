"""Stable ProxyDatabase façade composed from functional query modules."""

import sqlite3
from datetime import timedelta

from app.core import sqlite_runtime
from app.core.time import format_utc, utc_now
from app.db.migrations import TOKEN_BOARD_DATABASE_NAME
from app.db.proxy.accounts_read import ProxyAccountReadMixin
from app.db.proxy.accounts_write import ProxyAccountWriteMixin
from app.db.proxy.agent_subscriptions import ProxySubscriptionMixin
from app.db.proxy.agents import ProxyAgentMixin
from app.db.proxy.billing_ledger import ProxyBillingLedgerMixin
from app.db.proxy.billing_read import ProxyBillingReadMixin
from app.db.proxy.export import ProxyExportMixin
from app.db.proxy.lifecycle import ProxyLifecycleMixin
from app.db.proxy.performance import ProxyPerformanceMixin
from app.db.proxy.pricing import ProxyPricingMixin
from app.db.proxy.routing import ProxyRoutingMixin
from app.services.billing_report import actual_cost, live_request_sql


class ProxyDatabase(
        ProxyAccountReadMixin,
        ProxyAccountWriteMixin,
        ProxyLifecycleMixin,
        ProxyRoutingMixin,
        ProxyPricingMixin,
        ProxyBillingReadMixin,
        ProxyBillingLedgerMixin,
        ProxyExportMixin,
        ProxyPerformanceMixin,
        ProxyAgentMixin):
    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        from app.db.migrations import schema_dir_for
        from app.db.schema_upgrade import verify_current_database
        self.schema_dir = schema_dir or schema_dir_for(
            self.db_path, TOKEN_BOARD_DATABASE_NAME)
        verify_current_database(self.db_path, TOKEN_BOARD_DATABASE_NAME,
                                self.schema_dir)

    def _connect(self) -> sqlite3.Connection:
        return sqlite_runtime.connect(self.db_path, "proxy_runtime")

    @staticmethod
    def _next_shared_id(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "SELECT max(COALESCE((SELECT max(id) FROM accounts),0),"
            "COALESCE((SELECT max(id) FROM route_sets),0),"
            "COALESCE((SELECT max(id) FROM account_identities),0))+1"
        ).fetchone()[0])

    @staticmethod
    def _v1_route_account(conn: sqlite3.Connection, route_set_id: int):
        return conn.execute(
            "SELECT rs.id AS route_set_id,rs.account_id,u.id AS upstream_id "
            "FROM route_sets rs LEFT JOIN upstreams u ON u.account_id=rs.account_id "
            "AND u.enabled=1 LEFT JOIN accounts a ON a.id=rs.account_id "
            "WHERE rs.id=? AND (rs.account_id IS NULL OR a.account_kind='proxy') "
            "ORDER BY u.id LIMIT 1", (route_set_id,),
        ).fetchone()

    def get_stats(self) -> dict:
        conn = self._connect()
        try:
            now = utc_now()
            today = now.strftime("%Y-%m-%d")
            now_text = format_utc(now)
            start_text = format_utc(now - timedelta(days=30))
            usage = conn.execute(
                "SELECT COUNT(*) total_requests,COALESCE(SUM(total_tokens),0) total_tokens,"
                "COALESCE(SUM(equivalent_cost),0) theoretical_cost,"
                "COALESCE(SUM(billed_usage_cost),0) billed_usage_cost "
                "FROM request_log r " + live_request_sql("r"),
                {"start": start_text, "end": now_text, "now": now_text},
            ).fetchone()
            daily = conn.execute(
                "SELECT COUNT(*) requests,COALESCE(SUM(equivalent_cost),0) theoretical,"
                "COALESCE(SUM(billed_usage_cost),0) metered FROM request_log r "
                + live_request_sql("r") + " AND date(r.requested_at)=:today",
                {"start": f"{today}T00:00:00Z", "end": now_text,
                 "now": now_text, "today": today},
            ).fetchone()
            costs = actual_cost(conn, now=now, days=30)
            active_upstreams = conn.execute(
                "SELECT COUNT(DISTINCT r.account_id) FROM request_log r "
                "JOIN accounts a ON a.id=r.account_id "
                "WHERE a.account_kind='proxy' AND date(r.requested_at)=?", (today,),
            ).fetchone()[0]
            incomplete = conn.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE finalized_at IS NULL AND period_start<=? AND period_end>?",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"),) * 2,
            ).fetchone()[0] + conn.execute(
                "SELECT COUNT(*) FROM agent_subscription_period_charges "
                "WHERE finalized_at IS NULL AND period_start<=? AND period_end>?",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"),) * 2,
            ).fetchone()[0]
            return {
                "total_requests": usage["total_requests"],
                "today_requests": daily["requests"],
                "metered_cost": round(costs["metered_cost"], 4),
                "recurring_cost": round(costs["recurring_cost"], 4),
                "total_cost": round(costs["total_cost"], 4),
                "today_cost": round(daily["theoretical"], 4),
                "theoretical_cost": round(usage["theoretical_cost"], 4),
                "today_actual_cost": round(daily["metered"], 4),
                "total_tokens": usage["total_tokens"],
                "active_upstreams": active_upstreams,
                "active_accounts": conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE account_kind IN ('proxy','agent')"
                ).fetchone()[0],
                "billing_incomplete_count": incomplete,
                "billing_health": "degraded" if incomplete else "ok",
            }
        finally:
            conn.close()
