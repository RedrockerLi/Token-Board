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
        # Ensure new tables exist (C++ proxy also creates them, but Flask may connect first)
        conn.execute("""CREATE TABLE IF NOT EXISTS account_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL, model_id TEXT NOT NULL,
            UNIQUE(account_id, model_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS key_model_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id INTEGER NOT NULL, pattern TEXT NOT NULL,
            upstream_model TEXT NOT NULL,
            UNIQUE(key_id, pattern))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS model_map_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS model_map_template_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES model_map_templates(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            pattern TEXT NOT NULL,
            upstream_model TEXT NOT NULL)""")
        # Performance metrics (local-only, not synced)
        conn.execute("""CREATE TABLE IF NOT EXISTS perf_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            upstream_latency_ms INTEGER NOT NULL DEFAULT 0,
            total_latency_ms INTEGER NOT NULL DEFAULT 0,
            status_code INTEGER NOT NULL,
            is_error INTEGER NOT NULL DEFAULT 0,
            concurrent_count INTEGER NOT NULL DEFAULT 0,
            requested_at TEXT NOT NULL DEFAULT (datetime('now')))""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_events_time ON perf_events(requested_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS in_flight_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_key_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            is_streaming INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT (datetime('now')))""")

        # ── Triggers: automatic cost computation from model_pricing ──────
        conn.execute("""CREATE TRIGGER IF NOT EXISTS tr_request_log_insert
            AFTER INSERT ON request_log
            BEGIN
                UPDATE request_log SET cost = COALESCE((
                    SELECT (NEW.prompt_tokens / 1000000.0) * mp.input_price
                         + (NEW.completion_tokens / 1000000.0) * mp.output_price
                    FROM model_pricing mp
                    WHERE LOWER(NEW.model) GLOB LOWER(mp.model_pattern)
                    ORDER BY mp.id LIMIT 1
                ), 0.0) WHERE id = NEW.id AND NEW.prompt_tokens + NEW.completion_tokens > 0;
            END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS tr_pricing_insert
            AFTER INSERT ON model_pricing
            BEGIN
                UPDATE request_log SET cost = COALESCE((
                    SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                         + (request_log.completion_tokens / 1000000.0) * mp.output_price
                    FROM model_pricing mp
                    WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                    ORDER BY mp.id LIMIT 1
                ), 0.0);
            END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS tr_pricing_update
            AFTER UPDATE ON model_pricing
            BEGIN
                UPDATE request_log SET cost = COALESCE((
                    SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                         + (request_log.completion_tokens / 1000000.0) * mp.output_price
                    FROM model_pricing mp
                    WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                    ORDER BY mp.id LIMIT 1
                ), 0.0);
            END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS tr_pricing_delete
            AFTER DELETE ON model_pricing
            BEGIN
                UPDATE request_log SET cost = COALESCE((
                    SELECT (request_log.prompt_tokens / 1000000.0) * mp.input_price
                         + (request_log.completion_tokens / 1000000.0) * mp.output_price
                    FROM model_pricing mp
                    WHERE LOWER(request_log.model) GLOB LOWER(mp.model_pattern)
                    ORDER BY mp.id LIMIT 1
                ), 0.0);
            END""")
        conn.commit()
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
                "SELECT id, name, upstream_key, base_url, api_format, created_at "
                "FROM upstream_accounts ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_account(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts (name, upstream_key, base_url, api_format) "
                "VALUES (?, ?, ?, ?)",
                (
                    data["name"],
                    data["upstream_key"],
                    data.get("base_url", ""),
                    data.get("api_format", "openai"),
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
            for key in ("name", "upstream_key", "base_url", "api_format"):
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

    # ── Key Model Map ──────────────────────────────────────────────────

    def update_key_model_map(self, key_id: int, mappings: list[dict]) -> int:
        """Replace all mappings for a key. Each mapping: {pattern, upstream_model}."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM key_model_map WHERE key_id = ?", (key_id,))
            for m in mappings:
                conn.execute(
                    "INSERT OR IGNORE INTO key_model_map (key_id, pattern, upstream_model) VALUES (?,?,?)",
                    (key_id, m["pattern"], m["upstream_model"]),
                )
            conn.commit()
            self._schedule_config_sync()
            return len(mappings)
        finally:
            conn.close()

    def get_key_model_map(self, key_id: int) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, pattern, upstream_model FROM key_model_map WHERE key_id = ? ORDER BY id",
                (key_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def lookup_model_map(self, key_id: int, model: str) -> str | None:
        """Return upstream_model if pattern matches, else None. First match wins."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT pattern, upstream_model FROM key_model_map WHERE key_id = ? ORDER BY id",
                (key_id,),
            ).fetchall()
            import re
            for row in rows:
                try:
                    if re.search(row["pattern"], model):
                        return row["upstream_model"]
                except re.error:
                    pass
            return None
        finally:
            conn.close()

    # ── Model Map Templates ───────────────────────────────────────────

    def get_templates(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, sort_order FROM model_map_templates ORDER BY sort_order, id"
            ).fetchall()
            result = []
            for r in rows:
                entries = conn.execute(
                    "SELECT id, pattern, upstream_model, sort_order FROM model_map_template_entries WHERE template_id = ? ORDER BY sort_order, id",
                    (r["id"],),
                ).fetchall()
                result.append({"id": r["id"], "name": r["name"], "sort_order": r["sort_order"],
                               "entries": [dict(e) for e in entries]})
            return result
        finally:
            conn.close()

    def create_template(self, data: dict) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO model_map_templates (name) VALUES (?)", (data["name"],))
            tid = cursor.lastrowid
            for i, e in enumerate(data.get("entries", [])):
                conn.execute(
                    "INSERT INTO model_map_template_entries (template_id, sort_order, pattern, upstream_model) VALUES (?,?,?,?)",
                    (tid, i, e["pattern"], e["upstream_model"]))
            conn.commit()
            self._schedule_config_sync()
            return tid
        finally:
            conn.close()

    def update_template(self, tid: int, data: dict) -> bool:
        conn = self._connect()
        try:
            if "name" in data:
                conn.execute("UPDATE model_map_templates SET name=? WHERE id=?", (data["name"], tid))
            if "entries" in data:
                conn.execute("DELETE FROM model_map_template_entries WHERE template_id=?", (tid,))
                for i, e in enumerate(data["entries"]):
                    conn.execute(
                        "INSERT INTO model_map_template_entries (template_id, sort_order, pattern, upstream_model) VALUES (?,?,?,?)",
                        (tid, i, e["pattern"], e["upstream_model"]))
            if "sort_order" in data:
                conn.execute("UPDATE model_map_templates SET sort_order=? WHERE id=?", (data["sort_order"], tid))
            conn.commit()
            self._schedule_config_sync()
            return True
        finally:
            conn.close()

    def delete_template(self, tid: int) -> bool:
        conn = self._connect()
        try:
            # Auto-detach any keys referencing this template
            conn.execute(
                "UPDATE local_keys SET template_id = NULL WHERE template_id = ?",
                (tid,),
            )
            conn.execute("DELETE FROM model_map_templates WHERE id=?", (tid,))
            conn.commit()
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def reorder_template_entries(self, tid: int, entry_id: int, direction: str) -> bool:
        conn = self._connect()
        try:
            entries = conn.execute(
                "SELECT id, sort_order FROM model_map_template_entries WHERE template_id=? ORDER BY sort_order, id",
                (tid,)).fetchall()
            idx = next((i for i, e in enumerate(entries) if e["id"] == entry_id), None)
            if idx is None: return False
            if direction == "up" and idx > 0:
                a, b = entries[idx - 1], entries[idx]
                conn.execute("UPDATE model_map_template_entries SET sort_order=? WHERE id=?", (b["sort_order"], a["id"]))
                conn.execute("UPDATE model_map_template_entries SET sort_order=? WHERE id=?", (a["sort_order"], b["id"]))
            elif direction == "down" and idx < len(entries) - 1:
                a, b = entries[idx], entries[idx + 1]
                conn.execute("UPDATE model_map_template_entries SET sort_order=? WHERE id=?", (b["sort_order"], a["id"]))
                conn.execute("UPDATE model_map_template_entries SET sort_order=? WHERE id=?", (a["sort_order"], b["id"]))
            conn.commit()
            self._schedule_config_sync()
            return True
        finally:
            conn.close()

    # ── Local API Keys ─────────────────────────────────────────────────

    def get_keys(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT k.id, k.key_value, k.label, k.account_id, k.template_id, "
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
            self._schedule_config_sync()
            return key_value
        finally:
            conn.close()

    def update_key(self, key_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("label", "account_id", "template_id"):
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
        """Hard-delete a local key and its model mappings."""
        conn = self._connect()
        try:
            # Check for request_log references (FK constraint)
            refs = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE local_key_id = ?",
                (key_id,),
            ).fetchone()[0]
            if refs > 0:
                raise ValueError(f"无法删除：该密钥有 {refs} 条请求记录，请先清理相关日志")

            # Clean up associated model map entries
            conn.execute("DELETE FROM key_model_map WHERE key_id = ?", (key_id,))
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
            # tr_pricing_insert trigger auto-recalculates all costs
            self._schedule_config_sync()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_pricing(self, pricing_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
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
            # tr_pricing_update trigger auto-recalculates all costs
            self._schedule_config_sync()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_pricing(self, pricing_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM model_pricing WHERE id = ?", (pricing_id,))
            conn.commit()
            # tr_pricing_delete trigger auto-recalculates all costs
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
            # tr_pricing_update triggers fire on the ID swap, auto-recalculating all costs
            self._schedule_config_sync()
            return True
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

    def get_daily_billing_by_model(self, year: int, month: int) -> list[dict]:
        """Daily billing breakdown with input/output token split (for stacked bar chart)."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(SUM(r.prompt_tokens), 0) AS input_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS output_tokens,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.cost), 0) AS cost
                FROM request_log r
                WHERE CAST(strftime('%Y', r.requested_at) AS INTEGER) = ?
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
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
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

            # Write to dashboard.db (only usage; cost_entry computed by triggers)
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
                    request_count=r["request_count"],
                )

            # Always sync model_pricing — triggers recalculate all costs.
            # Do this even when there are no new rows, so pricing changes
            # take effect on the next export.
            _sync_pricing_to_dashboard(conn, dash_db_path)

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
        """Aggregated performance stats for the last N minutes."""
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM perf_events "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            errors = conn.execute(
                "SELECT COUNT(*) FROM perf_events "
                "WHERE is_error = 1 AND requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            peak_concurrent = conn.execute(
                "SELECT COALESCE(MAX(concurrent_count), 0) FROM perf_events "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            avg_latency = conn.execute(
                "SELECT COALESCE(AVG(total_latency_ms), 0) FROM perf_events "
                "WHERE is_error = 0 AND requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),)
            ).fetchone()[0]

            return {
                "total_requests": total,
                "error_count": errors,
                "success_rate": round((total - errors) / max(total, 1) * 100, 1),
                "total_tokens": tokens,
                "peak_concurrent": peak_concurrent,
                "avg_latency_ms": round(avg_latency, 1),
            }
        finally:
            conn.close()

    def get_perf_latency(self, window_minutes: int = 60) -> list[dict]:
        """Latency percentiles (P50/P95/P99) per 1-minute bucket."""
        conn = self._connect()
        try:
            buckets = conn.execute(
                "SELECT DISTINCT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket "
                "FROM perf_events "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "ORDER BY bucket",
                (str(window_minutes),)
            ).fetchall()

            result = []
            for (bucket,) in buckets:
                rows = conn.execute(
                    "SELECT total_latency_ms, upstream_latency_ms FROM perf_events "
                    "WHERE strftime('%Y-%m-%d %H:%M', requested_at) = ? "
                    "ORDER BY total_latency_ms",
                    (bucket,)
                ).fetchall()

                if not rows:
                    continue

                total_vals = [r[0] for r in rows]
                upstream_vals = [r[1] for r in rows]
                n = len(total_vals)

                def percentile(sorted_vals, p):
                    k = max(0, min(n - 1, int(n * p / 100)))
                    return sorted_vals[k]

                result.append({
                    "bucket": bucket,
                    "p50_total": percentile(total_vals, 50),
                    "p95_total": percentile(total_vals, 95),
                    "p99_total": percentile(total_vals, 99),
                    "p50_upstream": percentile(upstream_vals, 50),
                    "p95_upstream": percentile(upstream_vals, 95),
                    "p99_upstream": percentile(upstream_vals, 99),
                    "count": n,
                })
            return result
        finally:
            conn.close()

    def get_perf_throughput(self, window_minutes: int = 60) -> list[dict]:
        """Request count and peak concurrency per 1-minute bucket."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket, "
                "COUNT(*) AS request_count, "
                "MAX(concurrent_count) AS peak_concurrent "
                "FROM perf_events "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "GROUP BY bucket "
                "ORDER BY bucket",
                (str(window_minutes),)
            ).fetchall()

            return [{
                "bucket": r[0],
                "requests": r[1],
                "peak_concurrent": r[2],
            } for r in rows]
        finally:
            conn.close()

    def get_perf_models(self, window_minutes: int = 60) -> list[dict]:
        """Per-model performance breakdown for the last N minutes."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*) AS request_count, "
                "AVG(CASE WHEN is_error = 0 THEN upstream_latency_ms END) AS avg_latency_ms, "
                "MAX(CASE WHEN is_error = 0 THEN upstream_latency_ms END) AS max_latency_ms, "
                "SUM(CASE WHEN is_error = 0 THEN 1 ELSE 0 END) AS success_count, "
                "SUM(is_error) AS error_count "
                "FROM perf_events "
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

    def get_perf_success_rate_history(self, window_minutes: int = 60) -> list[dict]:
        """Per-minute success rate for the last N minutes (for line chart)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket, "
                "COUNT(*) AS total, "
                "SUM(is_error) AS errors "
                "FROM perf_events "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "GROUP BY bucket "
                "ORDER BY bucket",
                (str(window_minutes),)
            ).fetchall()

            return [{
                "bucket": r[0],
                "total": r[1],
                "errors": r[2],
                "success_rate": round((r[1] - r[2]) / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_realtime(self, window_seconds: int = 60) -> dict:
        """Real-time metrics: current RPM estimate and live concurrency.

        Concurrency is read directly from the in_flight_requests table,
        which is maintained by the C++ proxy (INSERT on start, DELETE on end).
        """
        conn = self._connect()
        try:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM perf_events "
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
    """Copy model_pricing from proxy.db to dashboard.db.

    Replaces all pricing rows in dashboard. The model_pricing triggers
    in dashboard.db automatically recalculate all cost_entry rows after
    each pricing change.
    """
    import sqlite3

    dash_conn = sqlite3.connect(dash_db_path, timeout=5)
    dash_conn.execute("PRAGMA busy_timeout=5000")
    try:
        # Ensure table and triggers exist (dashboard may be freshly downloaded)
        dash_conn.execute("""CREATE TABLE IF NOT EXISTS model_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_pattern TEXT NOT NULL UNIQUE,
            input_price REAL NOT NULL,
            output_price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY')""")

        pricing_rows = proxy_conn.execute(
            "SELECT id, model_pattern, input_price, output_price, currency "
            "FROM model_pricing ORDER BY id"
        ).fetchall()

        # Replace all pricing wrapped in a transaction for atomicity
        dash_conn.execute("BEGIN")
        dash_conn.execute("DELETE FROM model_pricing")
        for row in pricing_rows:
            dash_conn.execute(
                "INSERT INTO model_pricing (id, model_pattern, input_price, output_price, currency) "
                "VALUES (?,?,?,?,?)",
                tuple(row),
            )
        dash_conn.commit()
    finally:
        dash_conn.close()

