"""Python-side SQLite access layer for the proxy tables.

Used by the Flask dashboard to manage upstream accounts, local API keys,
model pricing, and to read billing/usage data written by the C++ proxy.

Thread-safe: each method opens its own connection (SQLite in WAL mode
supports concurrent readers alongside a single writer).
"""

import secrets
import sqlite3
import string
from datetime import datetime, timezone


def _generate_key() -> str:
    """Generate a local proxy key: 'tb-' + 32 random hex chars."""
    return "tb-" + secrets.token_hex(16)


class ProxyDatabase:
    """Manages the proxy SQLite database from the Flask side."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return dashboard overview stats."""
        conn = self._connect()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            total_requests = conn.execute(
                "SELECT COUNT(*) FROM request_log"
            ).fetchone()[0]

            today_requests = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE date(requested_at) = ?",
                (today,),
            ).fetchone()[0]

            total_cost = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM request_log"
            ).fetchone()[0]

            today_cost = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM request_log WHERE date(requested_at) = ?",
                (today,),
            ).fetchone()[0]

            total_tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM request_log"
            ).fetchone()[0]

            total_keys = conn.execute(
                "SELECT COUNT(*) FROM local_keys"
            ).fetchone()[0]

            total_accounts = conn.execute(
                "SELECT COUNT(*) FROM upstream_accounts"
            ).fetchone()[0]

            return {
                "total_requests": total_requests,
                "today_requests": today_requests,
                "total_cost": round(total_cost, 4),
                "today_cost": round(today_cost, 4),
                "total_tokens": total_tokens,
                "active_keys": total_keys,
                "active_accounts": total_accounts,
            }
        finally:
            conn.close()

    # ── Upstream Accounts ──────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, upstream_key, base_url, created_at "
                "FROM upstream_accounts ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_account(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts (name, upstream_key, base_url) "
                "VALUES (?, ?, ?)",
                (
                    data["name"],
                    data["upstream_key"],
                    data.get("base_url", ""),
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("name", "upstream_key", "base_url"):
                if key in data:
                    fields.append(f"{key} = ?")
                    values.append(data[key])
            if not fields:
                return False
            values.append(account_id)
            conn.execute(
                f"UPDATE upstream_accounts SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_account(self, account_id: int) -> dict:
        """Hard-delete an account. Returns {ok: bool, error: str}."""
        conn = self._connect()
        try:
            # Check if any local keys reference this account
            refs = conn.execute(
                "SELECT COUNT(*) FROM local_keys WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            if refs > 0:
                return {"ok": False, "error": f"无法删除：有 {refs} 个本地密钥仍在使用此账户，请先删除相关密钥"}

            conn.execute("DELETE FROM upstream_accounts WHERE id = ?", (account_id,))
            conn.commit()
            return {"ok": conn.total_changes > 0, "error": ""}
        finally:
            conn.close()

    # ── Local API Keys ─────────────────────────────────────────────────

    def get_keys(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT k.id, k.key_value, k.label, k.account_id, "
                "a.name AS account_name, k.created_at, k.last_used_at "
                "FROM local_keys k "
                "LEFT JOIN upstream_accounts a ON k.account_id = a.id "
                "ORDER BY k.id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_key(self, data: dict) -> str:
        """Create a new local key. Returns the generated key value."""
        key_value = _generate_key()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO local_keys (key_value, label, account_id) "
                "VALUES (?, ?, ?)",
                (key_value, data.get("label", ""), data["account_id"]),
            )
            conn.commit()
            return key_value
        finally:
            conn.close()

    def update_key(self, key_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("label", "account_id"):
                if key in data:
                    fields.append(f"{key} = ?")
                    values.append(data[key])
            if not fields:
                return False
            values.append(key_id)
            conn.execute(
                f"UPDATE local_keys SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_key(self, key_id: int) -> bool:
        """Hard-delete a local key."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM local_keys WHERE id = ?", (key_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Model Pricing ──────────────────────────────────────────────────

    def get_pricing(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, model_pattern, input_price, output_price, currency "
                "FROM model_pricing ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_pricing(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO model_pricing (model_pattern, input_price, output_price) "
                "VALUES (?, ?, ?)",
                (data["model_pattern"], data["input_price"], data["output_price"]),
            )
            conn.commit()
            # Recalculate costs for matching existing requests
            self._recalculate_costs(conn, data["model_pattern"],
                                    data["input_price"], data["output_price"])
            return cursor.lastrowid
        finally:
            conn.close()

    def update_pricing(self, pricing_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            # Fetch the pattern before updating
            pattern = conn.execute(
                "SELECT model_pattern FROM model_pricing WHERE id = ?", (pricing_id,)
            ).fetchone()

            fields = []
            values = []
            for key in ("model_pattern", "input_price", "output_price"):
                if key in data:
                    fields.append(f"{key} = ?")
                    values.append(data[key])
            if not fields:
                return False
            values.append(pricing_id)
            conn.execute(
                f"UPDATE model_pricing SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()

            # Recalculate costs for matching requests
            if pattern:
                self._recalculate_costs(conn, data.get("model_pattern", pattern[0]),
                                        data.get("input_price"), data.get("output_price"))
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_pricing(self, pricing_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM model_pricing WHERE id = ?", (pricing_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Billing / Usage ────────────────────────────────────────────────

    def get_billing_summary(
        self,
        account_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Aggregated billing by account + model + date."""
        conn = self._connect()
        try:
            sql = """
                SELECT
                    a.name AS account_name,
                    r.account_id,
                    r.model,
                    date(r.requested_at) AS date,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(r.cost), 0) AS cost
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE 1=1
            """
            params = []
            if account_id:
                sql += " AND r.account_id = ?"
                params.append(account_id)
            if date_from:
                sql += " AND date(r.requested_at) >= ?"
                params.append(date_from)
            if date_to:
                sql += " AND date(r.requested_at) <= ?"
                params.append(date_to)

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
    ) -> dict:
        """Paginated request log with filters."""
        conn = self._connect()
        try:
            where = ["1=1"]
            params = []

            if account_id:
                where.append("r.account_id = ?")
                params.append(account_id)
            if model:
                where.append("r.model LIKE ?")
                params.append(f"%{model}%")
            if date_from:
                where.append("date(r.requested_at) >= ?")
                params.append(date_from)
            if date_to:
                where.append("date(r.requested_at) <= ?")
                params.append(date_to)

            where_clause = " AND ".join(where)

            # Total count
            total = conn.execute(
                f"SELECT COUNT(*) FROM request_log r WHERE {where_clause}",
                params,
            ).fetchone()[0]

            # Paginated data
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""SELECT
                    r.id, r.account_id, a.name AS account_name,
                    r.model, r.prompt_tokens, r.completion_tokens,
                    r.total_tokens, r.cost, r.is_streaming,
                    r.status_code, r.duration_ms, r.requested_at
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE {where_clause}
                ORDER BY r.requested_at DESC
                LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

            return {
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
                "items": [dict(r) for r in rows],
            }
        finally:
            conn.close()

    def get_billing_by_account(self) -> list[dict]:
        """Per-account cost summary."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    a.id AS account_id,
                    a.name AS account_name,
                    COUNT(*) AS total_requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(r.cost), 0) AS total_cost,
                    MAX(r.requested_at) AS last_used
                FROM upstream_accounts a
                LEFT JOIN request_log r ON a.id = r.account_id
                GROUP BY a.id, a.name
                ORDER BY total_cost DESC
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_daily_billing(self, year: int, month: int) -> list[dict]:
        """Daily billing breakdown for a specific month."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    a.name AS account_name,
                    COALESCE(SUM(r.cost), 0) AS cost,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE CAST(strftime('%Y', r.requested_at) AS INTEGER) = ?
                  AND CAST(strftime('%m', r.requested_at) AS INTEGER) = ?
                GROUP BY date(r.requested_at), r.account_id
                ORDER BY date, r.account_id
            """, (year, month)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_available_proxy_months(self) -> list[dict]:
        """Return list of {year, month} that have proxy data."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT DISTINCT
                    CAST(strftime('%Y', requested_at) AS INTEGER) AS year,
                    CAST(strftime('%m', requested_at) AS INTEGER) AS month
                FROM request_log
                ORDER BY year, month
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_month(self, year: int, month: int, output_dir: str) -> dict:
        """Export proxy data for a month to DeepSeek-format CSV files.

        Produces amount-YYYY-M.csv and cost-YYYY-M.csv in output_dir.
        Returns {amount_path, cost_path, record_count}.
        """
        import csv
        import os

        os.makedirs(output_dir, exist_ok=True)

        conn = self._connect()
        try:
            # Aggregate by date + account + model
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    a.name AS account_name,
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens,
                    COUNT(*) AS request_count,
                    COALESCE(SUM(r.cost), 0) AS cost
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE CAST(strftime('%Y', r.requested_at) AS INTEGER) = ?
                  AND CAST(strftime('%m', r.requested_at) AS INTEGER) = ?
                GROUP BY date(r.requested_at), a.name, r.model
                ORDER BY date(r.requested_at), a.name, r.model
            """, (year, month)).fetchall()

            # Get pricing for price-per-token columns
            pricing = {p["model_pattern"]: p for p in self.get_pricing()}

            def _find_price(model: str, token_type: str) -> float:
                """Match model against pricing patterns."""
                for entry in pricing.values():
                    pattern = entry["model_pattern"]
                    if self._glob_match(pattern, model):
                        if token_type == "output_tokens":
                            return entry["output_price"]
                        else:
                            return entry["input_price"]
                return 0.001  # fallback

            # Write amount CSV
            amount_path = os.path.join(output_dir, f"amount-{year}-{month}.csv")
            with open(amount_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["user_id", "utc_date", "model", "api_key_name",
                                 "api_key", "type", "price", "amount"])
                for r in rows:
                    name = r["account_name"] or "unknown"
                    model = r["model"]

                    # Output tokens
                    if r["completion_tokens"] > 0:
                        price = _find_price(model, "output_tokens")
                        writer.writerow([name, r["date"], model, name, "proxy",
                                         "output_tokens", price, r["completion_tokens"]])
                    # Input tokens (all as cache_miss since we don't track hit/miss)
                    if r["prompt_tokens"] > 0:
                        price = _find_price(model, "input_cache_miss_tokens")
                        writer.writerow([name, r["date"], model, name, "proxy",
                                         "input_cache_miss_tokens", price, r["prompt_tokens"]])
                    # Request count
                    if r["request_count"] > 0:
                        writer.writerow([name, r["date"], model, name, "proxy",
                                         "request_count", 0, r["request_count"]])

            # Write cost CSV
            cost_path = os.path.join(output_dir, f"cost-{year}-{month}.csv")
            with open(cost_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["user_id", "utc_date", "model", "wallet_type",
                                 "cost", "currency"])
                for r in rows:
                    if r["cost"] > 0:
                        name = r["account_name"] or "unknown"
                        writer.writerow([name, r["date"], r["model"], "proxy",
                                         round(r["cost"], 6), "CNY"])

            return {
                "amount_path": amount_path,
                "cost_path": cost_path,
                "record_count": len(rows),
            }
        finally:
            conn.close()

    def _recalculate_costs(self, conn, pattern: str,
                           input_price: float | None,
                           output_price: float | None):
        """Recalculate cost for all request_log entries matching a pricing pattern."""
        if input_price is None and output_price is None:
            return

        # Get current prices if only one changed
        row = conn.execute(
            "SELECT input_price, output_price FROM model_pricing WHERE model_pattern = ?",
            (pattern,),
        ).fetchone()
        if not row:
            return
        inp = input_price if input_price is not None else row["input_price"]
        out = output_price if output_price is not None else row["output_price"]

        # Find matching models
        models = conn.execute("SELECT DISTINCT model FROM request_log").fetchall()
        for (model,) in models:
            if self._glob_match(pattern, model):
                conn.execute(
                    """UPDATE request_log
                       SET cost = (prompt_tokens / 1000000.0) * ? + (completion_tokens / 1000000.0) * ?
                       WHERE model = ?""",
                    (inp, out, model),
                )
        conn.commit()

    @staticmethod
    def _glob_match(pattern: str, model: str) -> bool:
        """Simple glob match (*, ? only)."""
        pi = mi = 0
        star = -1
        match_start = 0
        while mi < len(model):
            if pi < len(pattern) and (pattern[pi] == '?' or
                                       pattern[pi].lower() == model[mi].lower()):
                pi += 1
                mi += 1
            elif pi < len(pattern) and pattern[pi] == '*':
                star = pi
                match_start = mi
                pi += 1
            elif star != -1:
                pi = star + 1
                match_start += 1
                mi = match_start
            else:
                return False
        while pi < len(pattern) and pattern[pi] == '*':
            pi += 1
        return pi == len(pattern)
