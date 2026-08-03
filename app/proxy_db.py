"""Python-side SQLite access layer for the proxy tables.

Used by the Flask dashboard to manage upstream accounts, local API keys,
model pricing, and to read billing/usage data written by the C++ proxy.

Thread-safe: each method opens its own connection (SQLite in WAL mode
supports concurrent readers alongside a single writer).
"""

import secrets
import sqlite3
import string
import threading
from datetime import datetime, timezone


def _generate_key() -> str:
    """Generate a local proxy key: 'tb-' + 32 random hex chars."""
    return "tb-" + secrets.token_hex(16)


class ProxyDatabase:
    """Manages the proxy SQLite database from the Flask side."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/proxy/*.sql); apply
        # once at construction. Fails fast (create_app aborts) on error.
        from app.migrations import migrate, schema_dir_for
        migrate(self.db_path, schema_dir_for(self.db_path, "proxy"))
        self._sync_timer: threading.Timer | None = None

    def _schedule_config_sync(self):
        """Debounced auto-sync: wait 3s after last config change, then upload."""
        if self._sync_timer:
            self._sync_timer.cancel()
        self._sync_timer = threading.Timer(3.0, self._do_config_sync)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _do_config_sync(self):
        """Upload config to cloud in a background thread."""
        from app.sync import sync_config_upload
        try:
            sync_config_upload(self.db_path)
        except Exception:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
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

            # Real consumption includes plan subscriptions: for every month
            # a plan account was actually used, one monthly price is added.
            plan_subscription = conn.execute("""
                SELECT COALESCE(SUM(ua.monthly_price), 0)
                FROM (
                    SELECT DISTINCT r.account_id,
                           strftime('%Y-%m', r.requested_at) AS m
                    FROM request_log r
                    JOIN upstream_accounts ua2 ON r.account_id = ua2.id
                    WHERE COALESCE(ua2.account_type, 'api') = 'plan'
                ) t
                JOIN upstream_accounts ua ON ua.id = t.account_id
            """).fetchone()[0]
            total_cost += plan_subscription

            # Today's consumption = api billed today + plan virtual cost
            # today (the api-billed amount that the plan covered).
            today_cost = conn.execute(
                "SELECT COALESCE(SUM(cost + virtual_cost), 0) FROM request_log "
                "WHERE date(requested_at) = ?",
                (today,),
            ).fetchone()[0]

            total_tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM request_log"
            ).fetchone()[0]

            # "Active upstreams" = real upstream accounts (non-aggregate) that
            # handled at least one request today.
            active_upstreams = conn.execute(
                "SELECT COUNT(DISTINCT r.account_id) FROM request_log r "
                "JOIN upstream_accounts ua ON r.account_id = ua.id "
                "WHERE COALESCE(ua.is_aggregate, 0) = 0 "
                "  AND date(r.requested_at) = ?",
                (today,),
            ).fetchone()[0]

            total_accounts = conn.execute(
                "SELECT COUNT(*) FROM upstream_accounts WHERE is_aggregate = 0"
            ).fetchone()[0]

            return {
                "total_requests": total_requests,
                "today_requests": today_requests,
                "total_cost": round(total_cost, 4),
                "today_cost": round(today_cost, 4),
                "total_tokens": total_tokens,
                "active_upstreams": active_upstreams,
                "active_accounts": total_accounts,
            }
        finally:
            conn.close()

    # ── Upstream Accounts ──────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, upstream_key, base_url, api_format, "
                "COALESCE(endpoint_path,'') AS endpoint_path, "
                "COALESCE(auth_header,'bearer') AS auth_header, "
                "COALESCE(is_aggregate,0) AS is_aggregate, "
                "COALESCE(account_type,'api') AS account_type, "
                "COALESCE(monthly_price,0) AS monthly_price, "
                "max_concurrency, created_at "
                "FROM upstream_accounts ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_account(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts "
                "(name, upstream_key, base_url, api_format, endpoint_path, auth_header, "
                " account_type, monthly_price, max_concurrency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data["name"],
                    data["upstream_key"],
                    data.get("base_url", ""),
                    data.get("api_format", "openai"),
                    data.get("endpoint_path", ""),
                    data.get("auth_header", "bearer"),
                    data.get("account_type", "api"),
                    float(data.get("monthly_price", 0) or 0),
                    data.get("max_concurrency"),
                ),
            )
            conn.commit()
            self._schedule_config_sync()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("name", "upstream_key", "base_url", "api_format",
                        "endpoint_path", "auth_header", "account_type",
                        "monthly_price", "max_concurrency"):
                if key in data:
                    if key == "monthly_price":
                        val = float(data[key] or 0)
                    elif key == "max_concurrency":
                        val = data[key] if data[key] not in (None, "") else None
                    else:
                        val = data[key]
                    fields.append(f"{key} = ?")
                    values.append(val)
            if not fields:
                return False
            values.append(account_id)
            conn.execute(
                f"UPDATE upstream_accounts SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_account(self, account_id: int) -> dict:
        """Hard-delete an account. Returns {ok: bool, error: str}."""
        conn = self._connect()
        try:
            # Check if any local keys reference this account
            key_refs = conn.execute(
                "SELECT COUNT(*) FROM local_keys WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            if key_refs > 0:
                return {"ok": False, "error": f"无法删除：有 {key_refs} 个本地密钥仍在使用此账户，请先删除相关密钥"}

            # Check if any request logs reference this account
            log_refs = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            if log_refs > 0:
                return {"ok": False, "error": f"无法删除：有 {log_refs} 条请求记录关联此账户，请先清理相关日志"}

            conn.execute("DELETE FROM upstream_accounts WHERE id = ?", (account_id,))
            conn.commit()
            self._schedule_config_sync()
            return {"ok": conn.total_changes > 0, "error": ""}
        finally:
            conn.close()

    # ── Account Models ────────────────────────────────────────────────

    def update_account_models(self, account_id: int, models: list[str]) -> int:
        """Replace all models for an account. Returns count of models stored."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM account_models WHERE account_id = ?", (account_id,))
            for m in models:
                conn.execute(
                    "INSERT OR IGNORE INTO account_models (account_id, model_id) VALUES (?,?)",
                    (account_id, m),
                )
            conn.commit()
            self._schedule_config_sync()
            return len(models)
        finally:
            conn.close()

    def get_account_models(self, account_id: int) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT model_id FROM account_models WHERE account_id = ? ORDER BY id",
                (account_id,),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()

    # ── Aggregate Accounts ─────────────────────────────────────────────

    def get_aggregates(self) -> list[dict]:
        """Aggregate accounts (is_aggregate=1) with their model entries."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at FROM upstream_accounts "
                "WHERE is_aggregate = 1 ORDER BY id"
            ).fetchall()
            result = []
            for r in rows:
                entries = conn.execute(
                    "SELECT e.id, e.pattern, e.upstream_account_id, "
                    "a.name AS upstream_account_name, e.upstream_model "
                    "FROM aggregate_entries e "
                    "LEFT JOIN upstream_accounts a ON e.upstream_account_id = a.id "
                    "WHERE e.account_id = ? ORDER BY e.sort_order, e.id",
                    (r["id"],),
                ).fetchall()
                result.append({**dict(r), "entries": [dict(e) for e in entries]})
            return result
        finally:
            conn.close()

    def create_aggregate(self, data: dict) -> int:
        """Create an aggregate account (is_aggregate=1) + its model entries."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts "
                "(name, upstream_key, base_url, api_format, is_aggregate) "
                "VALUES (?, '', '', 'openai', 1)",
                (data["name"],),
            )
            agg_id = cursor.lastrowid
            for i, e in enumerate(data.get("entries", [])):
                conn.execute(
                    "INSERT INTO aggregate_entries "
                    "(account_id, sort_order, pattern, upstream_account_id, upstream_model) "
                    "VALUES (?,?,?,?,?)",
                    (agg_id, i, e["pattern"], e["account_id"], e["upstream_model"]),
                )
            conn.commit()
            self._schedule_config_sync()
            return agg_id
        finally:
            conn.close()

    def update_aggregate(self, agg_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            if "name" in data:
                conn.execute(
                    "UPDATE upstream_accounts SET name=? WHERE id=? AND is_aggregate=1",
                    (data["name"], agg_id),
                )
            if "entries" in data:
                conn.execute(
                    "DELETE FROM aggregate_entries WHERE account_id=?", (agg_id,))
                for i, e in enumerate(data["entries"]):
                    conn.execute(
                        "INSERT INTO aggregate_entries "
                        "(account_id, sort_order, pattern, upstream_account_id, upstream_model) "
                        "VALUES (?,?,?,?,?)",
                        (agg_id, i, e["pattern"], e["account_id"], e["upstream_model"]),
                    )
            conn.commit()
            self._schedule_config_sync()
            return True
        finally:
            conn.close()

    def delete_aggregate(self, agg_id: int) -> bool:
        """Delete an aggregate account. Entries cascade via FK."""
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM upstream_accounts WHERE id=? AND is_aggregate=1",
                (agg_id,),
            )
            conn.commit()
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Local API Keys ─────────────────────────────────────────────────

    def get_keys(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT k.id, k.key_value, k.label, k.account_id, "
                "a.name AS account_name, a.api_format AS account_format, "
                "k.created_at, k.last_used_at "
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
            self._schedule_config_sync()
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
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_key(self, key_id: int) -> bool:
        """Hard-delete a local key. Its request_log rows are kept and their
        local_key_id is set to NULL via the ON DELETE SET NULL foreign key,
        so usage/billing data is preserved."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM local_keys WHERE id = ?", (key_id,))
            conn.commit()
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Model Pricing ──────────────────────────────────────────────────

    def get_pricing(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, model_pattern, input_price, output_price, "
                "cache_read_price, currency "
                "FROM model_pricing ORDER BY id"
            ).fetchall()

            # Attach time-slot multipliers (start/end in UTC+0 minutes).
            slots_by_pricing: dict[int, list[dict]] = {}
            for s in conn.execute(
                "SELECT id, pricing_id, start_minute, end_minute, multiplier "
                "FROM pricing_slots ORDER BY pricing_id, id"
            ):
                slots_by_pricing.setdefault(s["pricing_id"], []).append({
                    "id": s["id"],
                    "start_minute": s["start_minute"],
                    "end_minute": s["end_minute"],
                    "multiplier": s["multiplier"],
                })
            return [dict(r) | {"slots": slots_by_pricing.get(r["id"], [])} for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _insert_pricing_slots(conn, pricing_id: int, slots) -> None:
        """Insert time-slot multipliers for a pricing row.

        slots: iterable of {start_minute, end_minute, multiplier} with
        boundaries in UTC+0 minutes ([0,1439]; start>end means overnight).
        """
        if not slots:
            return
        for s in slots:
            conn.execute(
                "INSERT INTO pricing_slots "
                "(pricing_id, start_minute, end_minute, multiplier) "
                "VALUES (?,?,?,?)",
                (pricing_id, int(s["start_minute"]), int(s["end_minute"]),
                 float(s.get("multiplier", 1.0))),
            )

    def create_pricing(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO model_pricing (model_pattern, input_price, output_price, cache_read_price) "
                "VALUES (?, ?, ?, ?)",
                (data["model_pattern"], data["input_price"], data["output_price"],
                 data.get("cache_read_price")),
            )
            pid = cursor.lastrowid
            # Cost is frozen at write time (tr_request_log_insert); time-slot
            # multipliers only affect future inserts.
            self._insert_pricing_slots(conn, pid, data.get("slots"))
            conn.commit()
            self._schedule_config_sync()
            return pid
        finally:
            conn.close()

    def update_pricing(self, pricing_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("model_pattern", "input_price", "output_price", "cache_read_price"):
                if key in data:
                    fields.append(f"{key} = ?")
                    values.append(data[key])
            if not fields and "slots" not in data:
                return False
            if fields:
                values.append(pricing_id)
                conn.execute(
                    f"UPDATE model_pricing SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
            if "slots" in data:
                # Replace the full slot set for this pricing row.
                conn.execute("DELETE FROM pricing_slots WHERE pricing_id = ?",
                             (pricing_id,))
                self._insert_pricing_slots(conn, pricing_id, data["slots"])
            conn.commit()
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_pricing(self, pricing_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM model_pricing WHERE id = ?", (pricing_id,))
            conn.commit()
            # pricing_slots rows are removed by ON DELETE CASCADE.
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def reorder_pricing(self, pid: int, direction: str) -> bool:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM model_pricing ORDER BY id").fetchall()
            idx = next((i for i, r in enumerate(rows) if r["id"] == pid), None)
            if idx is None: return False
            if direction == "up" and idx > 0:
                a, b = rows[idx - 1]["id"], rows[idx]["id"]
                conn.execute("UPDATE model_pricing SET id=-1 WHERE id=?", (a,))
                conn.execute("UPDATE model_pricing SET id=? WHERE id=?", (a, b))
                conn.execute("UPDATE model_pricing SET id=? WHERE id=?", (b, -1))
            elif direction == "down" and idx < len(rows) - 1:
                a, b = rows[idx]["id"], rows[idx + 1]["id"]
                conn.execute("UPDATE model_pricing SET id=-1 WHERE id=?", (a,))
                conn.execute("UPDATE model_pricing SET id=? WHERE id=?", (a, b))
                conn.execute("UPDATE model_pricing SET id=? WHERE id=?", (b, -1))
            conn.commit()
            # Id swap changes match priority (ORDER BY id LIMIT 1) for new
            # requests only; historical cost stays frozen at write time.
            self._schedule_config_sync()
            return True
        finally:
            conn.close()

    # ── Timeout config (per client wire format) ────────────────────────

    _TIMEOUT_GROUPS = ("anthropic", "openai_responses", "openai")
    _TIMEOUT_FIELDS = ("streaming_first_byte_timeout",
                       "streaming_idle_timeout",
                       "non_streaming_timeout")

    def get_timeout_config(self) -> list[dict]:
        """All proxy_timeout_config rows (one per client wire format)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT app_type, streaming_first_byte_timeout, "
                "streaming_idle_timeout, non_streaming_timeout "
                "FROM proxy_timeout_config ORDER BY app_type"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_timeout_config(self, app_type: str, data: dict) -> bool:
        """Validate and upsert one timeout group (all three fields required).

        Ranges mirror cc-switch's UI: first-byte 1-120, idle 0-600 (0 = disabled),
        non-streaming 60-1200.  Raises ValueError on invalid input.
        """
        if app_type not in self._TIMEOUT_GROUPS:
            raise ValueError(f"未知的线格式分组: {app_type}")
        values = {}
        for key in self._TIMEOUT_FIELDS:
            if key not in data:
                raise ValueError(f"缺少字段: {key}")
            try:
                v = int(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数")
            if key == "streaming_first_byte_timeout":
                if not (1 <= v <= 120):
                    raise ValueError("流式首字节超时范围 1-120 秒")
            elif key == "streaming_idle_timeout":
                if not (0 <= v <= 600):
                    raise ValueError("流式静默超时范围 0-600 秒（0=禁用）")
            else:
                if not (60 <= v <= 1200):
                    raise ValueError("非流式超时范围 60-1200 秒")
            values[key] = v
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO proxy_timeout_config "
                "(app_type, streaming_first_byte_timeout, streaming_idle_timeout, "
                " non_streaming_timeout) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(app_type) DO UPDATE SET "
                "  streaming_first_byte_timeout = excluded.streaming_first_byte_timeout, "
                "  streaming_idle_timeout = excluded.streaming_idle_timeout, "
                "  non_streaming_timeout = excluded.non_streaming_timeout",
                (app_type, values["streaming_first_byte_timeout"],
                 values["streaming_idle_timeout"], values["non_streaming_timeout"]),
            )
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
                WHERE COALESCE(a.is_aggregate, 0) = 0
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
                    r.model, r.prompt_tokens, r.cache_read_tokens,
                    r.completion_tokens,
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
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND CAST(strftime('%Y', r.requested_at) AS INTEGER) = ?
                  AND CAST(strftime('%m', r.requested_at) AS INTEGER) = ?
                GROUP BY date(r.requested_at), r.account_id
                ORDER BY date, r.account_id
            """, (year, month)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_daily_billing_by_model(self, year: int, month: int) -> list[dict]:
        """Daily billing breakdown with input/output token split (for stacked bar chart)."""
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
                    COALESCE(SUM(r.cost), 0) AS cost
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND CAST(strftime('%Y', r.requested_at) AS INTEGER) = ?
                  AND CAST(strftime('%m', r.requested_at) AS INTEGER) = ?
                GROUP BY date(r.requested_at)
                ORDER BY date
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

    def get_today_upstream_usage(self) -> list[dict]:
        """Per real upstream account used today: real/theoretical cost, tokens, requests.

        Active upstreams = non-aggregate upstream_accounts with at least one
        request_log row today (UTC day, same boundary as get_stats).
        """
        conn = self._connect()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT a.name AS account_name,
                       COALESCE(SUM(r.cost), 0) AS real_cost,
                       COALESCE(SUM(r.cost + r.virtual_cost), 0) AS theoretical_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
                JOIN upstream_accounts a ON r.account_id = a.id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND date(r.requested_at) = ?
                GROUP BY r.account_id
                ORDER BY theoretical_cost DESC
            """, (today,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_to_dashboard(self, mark_exported: bool = True) -> dict:
        """Export request_log rows to dashboard.db.

        Processes rows where exported IN (0, 1):
          0 = never exported
          1 = exported but upload may have failed → re-export to be safe

        Marks 0→1 after writing. Rows already at 1 stay at 1.
        Caller should transition 1→2 after confirming cloud upload.

        Always syncs model_pricing from proxy to dashboard.
        """
        import os

        conn = self._connect()
        try:
            # Query unexported rows, aggregate by date/account/model
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    a.name AS account_name,
                    COALESCE(a.account_type, 'api') AS account_type,
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(r.cost), 0) AS cost,
                    COUNT(*) AS request_count
                FROM request_log r
                LEFT JOIN upstream_accounts a ON r.account_id = a.id
                WHERE r.exported IN (0, 1)
                  AND LOWER(r.model) != 'unknown'
                  AND r.model != ''
                  AND a.name IS NOT NULL
                GROUP BY date(r.requested_at), a.name, r.model
                ORDER BY date(r.requested_at), a.name, r.model
            """).fetchall()

            # Write to dashboard.db (usage + cost_entry aggregated from the
            # frozen per-request cost recorded by the proxy).
            from app.dashboard_db import DashboardDatabase
            dash_db_path = os.path.join(
                os.path.dirname(self.db_path), "dashboard.db"
            )
            dash_db = DashboardDatabase(dash_db_path)
            dash_count = 0
            for r in rows:
                name = r["account_name"]
                model = r["model"]
                if not name or name.lower() == "unknown":
                    continue
                if not model or model.lower() == "unknown":
                    continue
                dash_count += dash_db.upsert_proxy_data(
                    date=r["date"],
                    model=model,
                    account_name=name,
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    cache_read_tokens=r["cache_read_tokens"],
                    request_count=r["request_count"],
                    cost=r["cost"],
                    account_type=r["account_type"],
                )

            # Mirror pricing config to dashboard (cost is frozen at write time;
            # this keeps the dashboard copy consistent for config sync).
            # Do this even when there are no new rows.
            _sync_pricing_to_dashboard(conn, dash_db_path)

            # ── Plan economics: full rewrite of per-month plan summary ──
            # subscription_cost = monthly price for each month the plan was
            # actually used; virtual_cost = api-billed amount of that usage.
            dash_db.clear_plan_summary()
            plan_rows = conn.execute("""
                SELECT
                    a.name AS account_name,
                    strftime('%Y-%m', r.requested_at) AS month,
                    COALESCE(a.monthly_price, 0) AS monthly_price,
                    COALESCE(SUM(r.virtual_cost), 0) AS virtual_cost
                FROM request_log r
                JOIN upstream_accounts a ON r.account_id = a.id
                WHERE COALESCE(a.account_type, 'api') = 'plan'
                  AND a.name IS NOT NULL
                GROUP BY a.name, month
                ORDER BY month, a.name
            """).fetchall()
            for pr in plan_rows:
                dash_db.upsert_plan_summary(
                    month=pr["month"],
                    account_name=pr["account_name"],
                    subscription_cost=float(pr["monthly_price"] or 0),
                    virtual_cost=float(pr["virtual_cost"] or 0),
                )

            # Mark 0→1 (rows that were just exported). Rows already at 1 stay at 1.
            # Caller transitions 1→2 after confirming cloud upload.
            if mark_exported:
                conn.execute("UPDATE request_log SET exported = 1 WHERE exported = 0")
                conn.commit()

            return {
                "record_count": len(rows),
                "dashboard_records": dash_count,
            }
        finally:
            conn.close()

    def mark_uploaded(self):
        """Transition exported=1 → exported=2 (confirmed uploaded to cloud).

        Called by sync_dashboard AFTER the cloud upload succeeds.
        Rows stay at exported=1 if the upload failed, and will be
        re-exported on the next sync.
        """
        conn = self._connect()
        try:
            conn.execute("UPDATE request_log SET exported = 2 WHERE exported = 1")
            conn.commit()
        finally:
            conn.close()

    # ── Request Log Cleanup ──────────────────────────────────────────

    def cleanup_exported_logs(self, max_exported: int = 10000) -> int:
        """Delete oldest uploaded (exported=2) request_log rows.

        Rows at exported=0 or exported=1 are never deleted — they still
        need to be exported or confirmed uploaded.
        """
        conn = self._connect()
        try:
            cursor = conn.execute("""
                DELETE FROM request_log WHERE exported = 2 AND id NOT IN (
                    SELECT id FROM request_log WHERE exported = 2
                    ORDER BY requested_at DESC LIMIT ?
                )
            """, (max_exported,))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    # ── Performance Metrics (local-only) ───────────────────────────────

    def get_perf_summary(self, window_minutes: int = 15) -> dict:
        """Aggregated performance stats for the last N minutes.

        Data source: request_log, which records every request outcome
        (including aborted/timeout/error attempts). Errors = status_code >= 400.
        """
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            errors = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE status_code >= 400 "
                "  AND requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            avg_latency = conn.execute(
                "SELECT COALESCE(AVG(duration_ms), 0) FROM request_log "
                "WHERE status_code < 400 "
                "  AND requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            return {
                "total_requests": total,
                "error_count": errors,
                "success_rate": round((total - errors) / max(total, 1) * 100, 1),
                "total_tokens": tokens,
                "avg_latency_ms": round(avg_latency, 1),
            }
        finally:
            conn.close()

    def get_perf_latency(self, window_minutes: int = 60) -> list[dict]:
        """Total-latency percentiles (P50/P95/P99 of duration_ms) per bucket.

        request_log carries only the total duration (no upstream/proxy split).
        """
        conn = self._connect()
        try:
            buckets = conn.execute(
                "SELECT DISTINCT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket "
                "FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "ORDER BY bucket",
                (str(window_minutes),)
            ).fetchall()

            result = []
            for (bucket,) in buckets:
                vals = [r[0] for r in conn.execute(
                    "SELECT duration_ms FROM request_log "
                    "WHERE strftime('%Y-%m-%d %H:%M', requested_at) = ? "
                    "ORDER BY duration_ms",
                    (bucket,)
                )]
                if not vals:
                    continue
                n = len(vals)

                def percentile(p):
                    k = max(0, min(n - 1, int(n * p / 100)))
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

    def get_perf_throughput(self, window_minutes: int = 60) -> list[dict]:
        """Request count per 1-minute bucket (from request_log)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket, "
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

    def get_perf_models(self, window_minutes: int = 60) -> list[dict]:
        """Per-model performance breakdown for the last N minutes (from request_log)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*) AS request_count, "
                "AVG(CASE WHEN status_code < 400 THEN duration_ms END) AS avg_latency_ms, "
                "MAX(CASE WHEN status_code < 400 THEN duration_ms END) AS max_latency_ms, "
                "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count "
                "FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "GROUP BY model "
                "ORDER BY request_count DESC",
                (str(window_minutes),)
            ).fetchall()

            return [{
                "model": r[0],
                "requests": r[1],
                "avg_latency_ms": round(r[2] or 0, 1),
                "max_latency_ms": r[3] or 0,
                "success_rate": round(r[4] / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_upstream_success_rate(self, window_minutes: int = 60) -> list[dict]:
        """Per real-upstream success rate for the last N minutes.

        'Real upstream' = non-aggregate upstream_accounts. Success = status < 400.
        Sourced from request_log, which records every request outcome.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT a.name AS account_name, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN r.status_code >= 400 THEN 1 ELSE 0 END) AS errors "
                "FROM request_log r "
                "JOIN upstream_accounts a ON r.account_id = a.id "
                "WHERE COALESCE(a.is_aggregate, 0) = 0 "
                "  AND r.requested_at >= datetime('now', '-' || ? || ' minutes') "
                "GROUP BY r.account_id "
                "ORDER BY total DESC",
                (str(window_minutes),)
            ).fetchall()

            return [{
                "account_name": r[0],
                "total": r[1],
                "errors": r[2],
                "success_rate": round((r[1] - r[2]) / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_realtime(self, window_seconds: int = 60) -> dict:
        """Real-time metrics: current RPM estimate and live concurrency.

        RPM is estimated from request_log; concurrency is read directly from
        the in_flight_requests table maintained by the C++ proxy.
        """
        conn = self._connect()
        try:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' seconds')",
                (str(window_seconds),)
            ).fetchone()[0]

            rpm = round(recent_count / max(window_seconds / 60.0, 0.1), 1)

            # Live concurrency: count rows currently in the in_flight_requests table
            latest_concurrent = conn.execute(
                "SELECT COUNT(*) FROM in_flight_requests"
            ).fetchone()[0]

            # Also return the in-flight request details for richer dashboards
            in_flight = conn.execute(
                "SELECT id, model, is_streaming, "
                "ROUND((julianday('now') - julianday(started_at)) * 86400) AS elapsed_s "
                "FROM in_flight_requests ORDER BY started_at"
            ).fetchall()

            return {
                "rpm": rpm,
                "recent_requests": recent_count,
                "latest_concurrent": latest_concurrent,
                "in_flight": [dict(r) for r in in_flight],
            }
        finally:
            conn.close()


def _sync_pricing_to_dashboard(proxy_conn, dash_db_path: str):
    """Copy model_pricing (+ pricing_slots) from proxy.db to dashboard.db.

    Replaces all pricing rows in dashboard. Since cost is now frozen at
    write time, this copy is for config mirroring / multi-machine sync only
    (dashboard cost_entry is aggregated from request_log.cost on export).
    """
    import sqlite3

    dash_conn = sqlite3.connect(dash_db_path, timeout=5)
    dash_conn.execute("PRAGMA busy_timeout=5000")
    try:
        # model_pricing (and pricing_slots) are guaranteed by the dashboard
        # migration (schema/dashboard/*.sql), applied when DashboardDatabase
        # is constructed in export_to_dashboard.
        pricing_rows = proxy_conn.execute(
            "SELECT id, model_pattern, input_price, output_price, "
            "cache_read_price, currency "
            "FROM model_pricing ORDER BY id"
        ).fetchall()
        slot_rows = proxy_conn.execute(
            "SELECT id, pricing_id, start_minute, end_minute, multiplier "
            "FROM pricing_slots ORDER BY pricing_id, id"
        ).fetchall()

        # Replace all pricing wrapped in a transaction for atomicity.
        dash_conn.execute("BEGIN")
        dash_conn.execute("DELETE FROM model_pricing")  # cascade removes slots
        for row in pricing_rows:
            dash_conn.execute(
                "INSERT INTO model_pricing (id, model_pattern, input_price, output_price, cache_read_price, currency) "
                "VALUES (?,?,?,?,?,?)",
                tuple(row),
            )
        for row in slot_rows:
            dash_conn.execute(
                "INSERT INTO pricing_slots (id, pricing_id, start_minute, end_minute, multiplier) "
                "VALUES (?,?,?,?,?)",
                tuple(row),
            )
        dash_conn.commit()
    finally:
        dash_conn.close()

