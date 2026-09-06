"""ProxyDatabase methods for ProxyBillingLedgerMixin."""

from app.core.time import format_utc, utc_now
from app.db.proxy.common import sqlite3, timedelta
from app.services.billing_units import BillingUnitResolver
from app.services.billing_report import live_request_sql


class ProxyBillingLedgerMixin:
    def get_recent_billing_days(self, days: int = 30) -> list[str]:
        """Distinct current-state request dates in the last *days* days."""
        conn = self._connect()
        try:
            now = utc_now()
            rows = conn.execute("""
                SELECT DISTINCT date(r.requested_at) AS d
                FROM request_log r
            """ + live_request_sql("r") + """
                ORDER BY d
            """, {
                "start": format_utc(now - timedelta(days=int(days))),
                "end": format_utc(now),
                "now": format_utc(now),
            }).fetchall()
            return [r["d"] for r in rows]
        finally:
            conn.close()

    def get_today_upstream_usage(self) -> list[dict]:
        """Per live proxy account or Agent software used today."""
        conn = self._connect()
        try:
            now = utc_now()
            today = now.strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT live_account.name AS account_name,
                       CASE WHEN live_account.account_kind='agent' THEN 'agent'
                            ELSE r.source_kind END AS source_kind,
                       COALESCE(SUM(r.billed_usage_cost), 0) AS real_cost,
                       COALESCE(SUM(r.equivalent_cost), 0) AS theoretical_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
            """ + live_request_sql("r") + """ AND date(r.requested_at)=:today
                GROUP BY live_account.id
                ORDER BY theoretical_cost DESC
            """, {"start": f"{today}T00:00:00Z", "end": format_utc(now),
                  "now": format_utc(now), "today": today}).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _plan_key_billing_meta(conn: sqlite3.Connection) -> list[dict]:
        """Build plan key lifecycles from synchronized credential metadata.

        A plan is priced by its configured key slots, not by whether this
        machine has filled in the local plaintext key.  Cloud-only rows are
        therefore billable metadata; they still cannot route until a local
        secret is entered. Duplicate local+cloud rows with the same masked
        identity are collapsed to the local slot.
        """
        now = utc_now()
        return [{
            "account_id": unit.account_id,
            "contract_id": unit.contract_id,
            "credential_uuid": unit.credential_uuid,
            "key_id": unit.credential_runtime_id,
            "key_masked": unit.key_masked or "subscription",
            "billing_unit_id": unit.billing_unit_id.removeprefix("credential:"),
            "anchor": unit.valid_from,
            "end": unit.ends_at,
            "now": now,
            "currency": unit.currency,
        } for unit in BillingUnitResolver.proxy_units(conn, at=now)]
