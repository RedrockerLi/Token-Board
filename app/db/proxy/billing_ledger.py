"""ProxyDatabase methods for ProxyBillingLedgerMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


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
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT COALESCE(a.name, 'unknown') AS account_name,
                       CASE WHEN a.account_kind='agent' THEN 'agent'
                            ELSE r.source_kind END AS source_kind,
                       COALESCE(SUM(r.billed_usage_cost), 0) AS real_cost,
                       COALESCE(SUM(r.equivalent_cost), 0) AS theoretical_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE a.account_kind IN ('proxy','agent')
                  AND date(r.requested_at) = ?
                GROUP BY r.account_id
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
        now = _utc_now()
        result = []
        contracts = conn.execute(
                "SELECT bc.*,a.created_at,a.valid_from account_valid_from,a.deleted_at account_deleted_at,"
                "bc.valid_until contract_valid_until "
                "FROM billing_contracts bc JOIN accounts a ON a.id=bc.account_id "
                "WHERE bc.charge_type='recurring' "
                "AND a.account_kind='proxy' "
                "AND bc.valid_from<=?",
                (now.isoformat(timespec="seconds").replace("+00:00", "Z"),)
            ).fetchall()
        for contract in contracts:
            if contract["billing_scope"] == "credential":
                credentials = conn.execute(
                        "SELECT c.runtime_id,c.uuid,c.key_masked,c.created_at,c.valid_from,c.deleted_at "
                        "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id "
                        "WHERE u.account_id=? "
                        "AND (c.disabled_at IS NULL OR c.disabled_at>?) "
                        "AND (c.deleted_at IS NULL OR c.deleted_at>?) "
                        "ORDER BY c.position,c.runtime_id",
                        (contract["account_id"], now.isoformat(
                            timespec="seconds").replace("+00:00", "Z"),
                         now.isoformat(timespec="seconds").replace("+00:00", "Z")),
                    ).fetchall()
                seen_masks: set[tuple[int, str]] = set()
                for credential in credentials:
                    identity = (contract["account_id"], credential["key_masked"])
                    if identity in seen_masks:
                        continue
                    seen_masks.add(identity)
                    anchor = (_parse_iso_date(credential["valid_from"])
                              or _parse_utc_timestamp(credential["created_at"]).date())
                    end = min((value for value in (
                        _parse_utc_timestamp(credential["deleted_at"]),
                        _parse_utc_timestamp(contract["contract_valid_until"]),
                        _parse_utc_timestamp(contract["account_deleted_at"]))
                               if value is not None), default=None)
                    result.append({
                            "account_id": contract["account_id"],
                            "contract_id": contract["id"],
                            "credential_uuid": credential["uuid"],
                            "key_id": credential["runtime_id"],
                            "key_masked": credential["key_masked"],
                            "billing_unit_id": credential["uuid"],
                            "anchor": anchor, "end": end, "now": now,
                            "currency": contract["currency"],
                    })
            else:
                anchor = (_parse_iso_date(contract["account_valid_from"])
                          or _parse_utc_timestamp(contract["created_at"]).date())
                result.append({
                        "account_id": contract["account_id"],
                        "contract_id": contract["id"],
                        "credential_uuid": None, "key_id": None,
                        "key_masked": "subscription",
                        "billing_unit_id": f"contract:{contract['uuid']}",
                        "anchor": anchor,
                        "end": min((value for value in (
                            _parse_utc_timestamp(contract["contract_valid_until"]),
                            _parse_utc_timestamp(contract["account_deleted_at"]))
                            if value is not None), default=None),
                        "now": now, "currency": contract["currency"],
                    })
        for meta in result:
            meta["now"] = now
        return result
