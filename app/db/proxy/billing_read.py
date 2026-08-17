"""ProxyDatabase methods for ProxyBillingReadMixin."""

from app.db.proxy.common import *  # noqa: F401,F403


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
    def get_plan_billing_config(self) -> dict:
        conn = self._connect()
        try:
            row = self._billing_config_conn(conn)
            return {
                "price_change_effective": row["price_change_effective"],
                "cancellation_mode": row["cancellation_mode"],
                "timezone": "UTC",
            }
        finally:
            conn.close()

    def update_plan_billing_config(self, data: dict) -> bool:
        mode = data.get("price_change_effective")
        if mode not in ("current_period", "next_period"):
            raise ValueError("价格生效方式必须是 current_period 或 next_period")
        cancellation = data.get("cancellation_mode")
        if cancellation not in ("immediate", "end_of_period"):
            raise ValueError("删除默认操作必须是 immediate 或 end_of_period")
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO sync_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [("billing.price_change_effective", mode),
                 ("billing.cancellation_mode", cancellation)],
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
        """Aggregated billing by account + model + date.

        Defaults to the last *days* days (rolling 30d window). Groups by the
        account_name snapshot, so deleted accounts' history is preserved.
        """
        conn = self._connect()
        try:
            sql = """
                SELECT
                    COALESCE(a.name, 'unknown') AS account_name,
                    r.account_id,
                    r.model,
                    date(r.requested_at) AS date,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE 1=1
                  AND a.id IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM billing_contracts bc
                                 WHERE bc.account_id=r.account_id
                                 AND bc.charge_type='recurring'
                                 AND bc.valid_from<=r.requested_at
                                 AND (bc.valid_until IS NULL
                                      OR bc.valid_until>r.requested_at))
            """
            params = []
            if account_id:
                sql += " AND r.account_id = ?"
                params.append(account_id)
            if date_from:
                sql += " AND r.requested_at >= ?"
                params.append(_filter_bound(date_from))
            else:
                sql += " AND r.requested_at >= datetime('now', '-' || ? || ' days')"
                params.append(str(days))
            if date_to:
                sql += " AND r.requested_at <= ?"
                params.append(_filter_bound(date_to, end=True))

            sql += """
                GROUP BY r.account_id, r.model, date(r.requested_at)
                ORDER BY date(r.requested_at) DESC, r.account_id, r.model
            """
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_request_logs(
        self,
        page: int = 1,
        per_page: int = 50,
        account_id: int | None = None,
        model: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_attempts: bool = True,
        before_requested_at: str | None = None,
        before_id: int | None = None,
    ) -> dict:
        """Paginated request log with filters."""
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 200))
        conn = self._connect()
        try:
            # COUNT, page rows and optional attempt details must describe one
            # WAL read snapshot while the proxy appends logs concurrently.
            conn.execute("BEGIN")
            where = ["1=1"]
            params = []

            if account_id:
                where.append("r.account_id = ?")
                params.append(account_id)
            if model:
                where.append("r.model LIKE ?")
                params.append(f"%{model}%")
            if date_from:
                where.append("r.requested_at >= ?")
                params.append(_filter_bound(date_from))
            if date_to:
                where.append("r.requested_at <= ?")
                params.append(_filter_bound(date_to, end=True))
            if before_requested_at is not None and before_id is not None:
                where.append("(r.requested_at < ? OR "
                             "(r.requested_at = ? AND r.id < ?))")
                params.extend([before_requested_at, before_requested_at, before_id])

            where_clause = " AND ".join(where)

            # Total count
            total = conn.execute(
                f"SELECT COUNT(*) FROM request_log r WHERE {where_clause}",
                params,
            ).fetchone()[0]

            # Paginated data
            offset = 0 if before_requested_at is not None else (page - 1) * per_page
            rows = conn.execute(
                f"""SELECT
                    r.id, r.account_id, COALESCE(a.name, 'unknown') AS account_name,
                    r.model, r.prompt_tokens, r.cache_read_tokens,
                    r.completion_tokens,
                    r.total_tokens, r.equivalent_cost AS cost,
                    r.billed_usage_cost, r.pricing_status, r.pricing_rate_id,
                    r.queue_ms, r.accounting_ms, r.is_streaming,
                    r.status_code, r.ttft_ms, r.generation_ms, r.output_tps,
                    r.upstream_ttft_ms, r.upstream_duration_ms,
                    r.attempt_count, r.fallback_count,
                    r.requested_at
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE {where_clause}
                ORDER BY r.requested_at DESC, r.id DESC
                LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

            items = [dict(r) for r in rows]
            if items and include_attempts:
                ids = [item["id"] for item in items]
                placeholders = ",".join("?" for _ in ids)
                attempt_rows = conn.execute(
                    f"""SELECT t.request_log_id, t.attempt_index,
                               t.account_id, COALESCE(a.name, 'unknown') AS account_name,
                               t.upstream_key_id, t.status_code, t.duration_ms,
                               t.ttft_ms, t.is_timeout, t.error,
                               t.dns_ms,t.connect_ms,t.tls_ms,t.lease_wait_ms,
                               t.first_byte_ms,t.connection_reused
                        FROM request_attempts t
                        LEFT JOIN accounts a ON a.id = t.account_id
                        WHERE t.request_log_id IN ({placeholders})
                        ORDER BY t.request_log_id, t.attempt_index""",
                    ids,
                ).fetchall()
                by_request = {request_id: [] for request_id in ids}
                for attempt in attempt_rows:
                    by_request[attempt["request_log_id"]].append(dict(attempt))
                for item in items:
                    item["attempts"] = by_request[item["id"]]

            result = {
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
                "items": items,
                "next_cursor": ({"requested_at": items[-1]["requested_at"],
                                 "id": items[-1]["id"]}
                                if len(items) == per_page else None),
            }
            conn.commit()
            return result
        finally:
            conn.close()

    def get_daily_billing(self, days: int = 30) -> list[dict]:
        """Daily actual usage cost plus a separate theoretical-cost field."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(a.name, 'unknown') AS account_name,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost,
                    CASE WHEN EXISTS(SELECT 1 FROM billing_contracts bc
                         WHERE bc.account_id=r.account_id
                         AND bc.charge_type='recurring')
                         THEN 'plan' ELSE 'api' END AS account_type,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens
                FROM request_log r
                LEFT JOIN accounts a ON a.id = r.account_id
                WHERE a.id IS NOT NULL
                  AND NOT EXISTS(SELECT 1 FROM billing_contracts bc
                                 WHERE bc.account_id=r.account_id
                                 AND bc.charge_type='recurring'
                                 AND bc.valid_from<=r.requested_at
                                 AND (bc.valid_until IS NULL
                                      OR bc.valid_until>r.requested_at))
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at), r.account_id
                ORDER BY date, r.account_id
            """, (str(days),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_daily_billing_by_model(self, days: int = 30) -> list[dict]:
        """Daily traffic with separate actual and theoretical cost fields."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(SUM(r.prompt_tokens), 0) AS input_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS output_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_hit_tokens,
                    MAX(COALESCE(SUM(r.prompt_tokens), 0) - COALESCE(SUM(r.cache_read_tokens), 0), 0) AS cache_miss_tokens,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.billed_usage_cost), 0) AS cost,
                    COALESCE(SUM(r.equivalent_cost), 0) AS equivalent_cost
                FROM request_log r
                WHERE EXISTS(SELECT 1 FROM accounts a WHERE a.id=r.account_id)
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at)
                ORDER BY date
            """, (str(days),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
