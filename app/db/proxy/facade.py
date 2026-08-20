"""Stable ProxyDatabase façade composed from functional query modules."""

from app.db.proxy.common import *  # noqa: F401,F403
from app.db.proxy.accounts_read import ProxyAccountReadMixin
from app.db.proxy.accounts_write import ProxyAccountWriteMixin
from app.db.proxy.lifecycle import ProxyLifecycleMixin
from app.db.proxy.routing import ProxyRoutingMixin
from app.db.proxy.pricing import ProxyPricingMixin
from app.db.proxy.billing_read import ProxyBillingReadMixin
from app.db.proxy.billing_ledger import ProxyBillingLedgerMixin
from app.db.proxy.export import ProxyExportMixin
from app.db.proxy.performance import ProxyPerformanceMixin


class ProxyDatabase(
        ProxyAccountReadMixin,
        ProxyAccountWriteMixin,
        ProxyLifecycleMixin,
        ProxyRoutingMixin,
        ProxyPricingMixin,
        ProxyBillingReadMixin,
        ProxyBillingLedgerMixin,
        ProxyExportMixin,
        ProxyPerformanceMixin):
    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/proxy/vN/*.sql); apply
        # once at construction. Fails fast (create_app aborts) on error.
        from app.db.migrations import MigrationError, migrate, schema_dir_for
        self.schema_dir = schema_dir or schema_dir_for(self.db_path, "proxy")
        migrate(self.db_path, self.schema_dir, "proxy")
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT major FROM schema_version WHERE id=1").fetchone()
        finally:
            conn.close()
        if not row or int(row[0]) != 1:
            raise MigrationError(
                "ProxyDatabase only accepts V1; run the offline/local schema "
                "upgrade coordinator before opening the application")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _next_shared_id(conn: sqlite3.Connection) -> int:
        """Allocate an integer in the compatibility ID namespace.

        V0 exposed accounts and aggregate route sets through one integer ID.
        V1 keeps those concepts separate, but allocating above both maxima
        preserves the existing HTTP contract without conflating them in the
        normalized foreign keys.
        """
        return int(conn.execute(
            "SELECT max(COALESCE((SELECT max(id) FROM accounts),0),"
            "COALESCE((SELECT max(id) FROM route_sets),0))+1"
        ).fetchone()[0])

    @staticmethod
    def _v1_route_account(conn: sqlite3.Connection, route_set_id: int):
        return conn.execute(
            "SELECT rs.id AS route_set_id,rs.account_id,u.id AS upstream_id "
            "FROM route_sets rs LEFT JOIN upstreams u ON u.account_id=rs.account_id "
            "AND u.enabled=1 WHERE rs.id=? ORDER BY u.id LIMIT 1",
            (route_set_id,),
        ).fetchone()

    def get_stats(self) -> dict:
        """Proxy billing overview — 30-day rolling window.

        Usage cards (``total_tokens``, ``total_requests``, ``today_requests``)
        cover ALL traffic, including recurring-plan accounts, so the overview
        matches the daily-usage chart. ``total_cost`` is actual cost: billed
        metered usage plus recurring charges normalized to the reporting
        currency (plan accounts produce no metered usage, so their requests do
        not affect it). ``today_cost`` is the separate theoretical cost: what
        the same traffic would cost at the selected historical model rates.
        These figures must never be added, because that would count the same
        usage twice.
        """
        conn = self._connect()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            usage = conn.execute(
                "SELECT COUNT(*) total_requests,COALESCE(SUM(total_tokens),0) total_tokens,"
                "COALESCE(SUM(r.billed_usage_cost),0) billed_usage_cost "
                "FROM request_log r "
                "WHERE r.requested_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')"
            ).fetchone()
            daily = conn.execute(
                "SELECT COUNT(*) today_requests,COALESCE(SUM(equivalent_cost),0) "
                "today_theoretical_cost,COALESCE(SUM(r.billed_usage_cost),0) "
                "today_billed_usage_cost FROM request_log r "
                "WHERE date(r.requested_at)=?",
                (today,),
            ).fetchone()
            recurring = conn.execute(
                "SELECT COALESCE(SUM(normalized_recurring_cost),0) "
                "FROM billing_period_charges "
                "WHERE period_start<=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "AND period_end>strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
            today_recurring = conn.execute(
                "SELECT COALESCE(SUM(normalized_recurring_cost),0) "
                "FROM billing_period_charges WHERE period_start<=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "AND period_end>strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
            theoretical = conn.execute(
                "SELECT COALESCE(SUM(equivalent_cost),0) FROM request_log "
                "WHERE requested_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')"
            ).fetchone()[0]
            active_upstreams = conn.execute(
                "SELECT COUNT(DISTINCT r.account_id) FROM request_log r JOIN accounts a "
                "ON a.id=r.account_id WHERE a.lifecycle_state='active' AND date(r.requested_at)=?",
                (today,),
            ).fetchone()[0]
            billing_incomplete = conn.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE period_start<=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "AND period_end>strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "AND finalized_at IS NULL "
                "AND (normalized_recurring_cost IS NULL "
                "OR (currency!='CNY' AND fx_rate_date!=date(period_start)))"
            ).fetchone()[0]
            active_accounts = conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE lifecycle_state='active'"
            ).fetchone()[0]
            return {
                "total_requests": usage["total_requests"],
                "today_requests": daily["today_requests"],
                "total_cost": round(usage["billed_usage_cost"] + recurring, 4),
                "today_cost": round(daily["today_theoretical_cost"], 4),
                "theoretical_cost": round(theoretical, 4),
                "today_actual_cost": round(daily["today_billed_usage_cost"] + today_recurring, 4),
                "total_tokens": usage["total_tokens"],
                "active_upstreams": active_upstreams,
                "active_accounts": active_accounts,
                "billing_incomplete_count": billing_incomplete,
                "billing_health": "degraded" if billing_incomplete else "ok",
            }
        finally:
            conn.close()



# A few legacy static helpers refer to ProxyDatabase by name.  Bind that
# compatibility name in each implementation module after the façade exists;
# no runtime behavior or public import changes.
import app.db.proxy.accounts_read as _proxy_accounts_read
_proxy_accounts_read.ProxyDatabase = ProxyDatabase
import app.db.proxy.accounts_write as _proxy_accounts_write
_proxy_accounts_write.ProxyDatabase = ProxyDatabase
import app.db.proxy.lifecycle as _proxy_lifecycle
_proxy_lifecycle.ProxyDatabase = ProxyDatabase
import app.db.proxy.routing as _proxy_routing
_proxy_routing.ProxyDatabase = ProxyDatabase
import app.db.proxy.pricing as _proxy_pricing
_proxy_pricing.ProxyDatabase = ProxyDatabase
import app.db.proxy.billing_read as _proxy_billing_read
_proxy_billing_read.ProxyDatabase = ProxyDatabase
import app.db.proxy.billing_ledger as _proxy_billing_ledger
_proxy_billing_ledger.ProxyDatabase = ProxyDatabase
import app.db.proxy.export as _proxy_export
_proxy_export.ProxyDatabase = ProxyDatabase
import app.db.proxy.performance as _proxy_performance
_proxy_performance.ProxyDatabase = ProxyDatabase
