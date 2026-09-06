"""ProxyDatabase methods for ProxyBillingLedgerMixin."""

from app.core.time import utc_now
from app.db.proxy.common import sqlite3
from app.services.billing_units import BillingUnitResolver


class ProxyBillingLedgerMixin:
    def get_recent_billing_days(self, days: int = 30) -> list[str]:
        """Distinct dates in the last *days* days that have proxy data."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT DISTINCT date(requested_at) AS d
                FROM request_log
                WHERE requested_at >= datetime('now', '-' || ? || ' days')
                ORDER BY d
            """, (str(days),)).fetchall()
            return [r["d"] for r in rows]
        finally:
            conn.close()

    def get_today_upstream_usage(self) -> list[dict]:
        """Per proxy account or agent software used today."""
        conn = self._connect()
        try:
            today = utc_now().strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT COALESCE(ai.name, a.name, 'unknown') AS account_name,
                       CASE WHEN COALESCE(ai.account_kind,a.account_kind)='agent' THEN 'agent'
                            ELSE r.source_kind END AS source_kind,
                       COALESCE(SUM(r.billed_usage_cost), 0) AS real_cost,
                       COALESCE(SUM(r.equivalent_cost), 0) AS theoretical_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                LEFT JOIN account_identities ai
                  ON ai.id=COALESCE(r.account_identity_id,r.account_id)
                WHERE COALESCE(ai.account_kind,a.account_kind) IN ('proxy','agent')
                  AND date(r.requested_at) = ?
                GROUP BY COALESCE(r.account_identity_id,r.account_id)
                ORDER BY theoretical_cost DESC
            """, (today,)).fetchall()
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
            "end": unit.valid_until,
            "now": now,
            "currency": unit.currency,
        } for unit in BillingUnitResolver.proxy_units(conn, at=now)]
