"""Python-side SQLite access layer for the proxy tables.

Used by the Flask dashboard to manage upstream accounts, local API keys,
model pricing, and to read billing/usage data written by the C++ proxy.

Thread-safe: each method opens its own connection (SQLite in WAL mode
supports concurrent readers alongside a single writer).
"""

import os
import secrets
import sqlite3
import string
from datetime import datetime, timezone


def _generate_key() -> str:
    """Generate a local proxy key: 'tb-' + 32 random hex chars."""
    return "tb-" + secrets.token_hex(16)


def mask_key(key: str) -> str:
    """Mask an upstream key for display: 'sk-abc…' (first 6 + '…' + last 4)."""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "…"
    return f"{key[:6]}…{key[-4:]}"


class ProxyDatabase:
    """Manages the proxy SQLite database from the Flask side."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/proxy/*.sql); apply
        # once at construction. Fails fast (create_app aborts) on error.
        from app.migrations import migrate, schema_dir_for
        migrate(self.db_path, schema_dir_for(self.db_path, "proxy"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Proxy billing overview — 30-day rolling window.

        total_cost (real) = api accounts' billed usage in the last 30 days
        (SUM(api_cost) where account_type='api'; plan carries virtual only in
        api_cost) + current month's plan subscription fees. Plan subscription
        = monthly_price × key count per plan account used this month. Past
        months' fees live frozen in the archive and are NOT recomputed here.
        today_cost (theoretical) = SUM(api_cost) today (api bill + plan's
        api-equivalent amount the plan covered).
        """
        conn = self._connect()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            total_requests = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE requested_at >= datetime('now', '-30 days')"
            ).fetchone()[0]

            today_requests = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE date(requested_at) = ?",
                (today,),
            ).fetchone()[0]

            # Real billed usage: api accounts only (plan accounts' api_cost is
            # their virtual/theoretical bill — never counted as real, which
            # would double-count their subscription).
            total_cost = conn.execute(
                "SELECT COALESCE(SUM(r.api_cost), 0) FROM request_log r "
                "JOIN upstream_accounts a ON a.id = r.account_id "
                "WHERE COALESCE(a.account_type, 'api') = 'api' "
                "  AND r.requested_at >= datetime('now', '-30 days')"
            ).fetchone()[0]

            # Current-month plan subscription: current monthly_price × key count
            # for plan accounts used this month (price edits affect only the
            # current month; past months are frozen in the archive). Key count
            # falls back to 1 (the account's legacy single upstream_key) when
            # the account has no upstream_keys rows.
            plan_subscription = conn.execute("""
                SELECT COALESCE(SUM(a.monthly_price * (
                    SELECT MAX(1, COUNT(*)) FROM upstream_keys k
                    WHERE k.account_id = a.id)), 0)
                FROM (
                    SELECT DISTINCT r.account_id
                    FROM request_log r
                    WHERE strftime('%Y-%m', r.requested_at) = strftime('%Y-%m', 'now')
                      AND r.account_id IS NOT NULL
                ) t
                JOIN upstream_accounts a ON a.id = t.account_id
                WHERE COALESCE(a.account_type, 'api') = 'plan'
                  AND a.deleted_at IS NULL
            """).fetchone()[0]
            total_cost += plan_subscription

            # Today's consumption = api billed today + plan virtual cost today
            # (the api-billed amount the plan covered).
            today_cost = conn.execute(
                "SELECT COALESCE(SUM(api_cost), 0) FROM request_log "
                "WHERE date(requested_at) = ?",
                (today,),
            ).fetchone()[0]

            total_tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM request_log "
                "WHERE requested_at >= datetime('now', '-30 days')"
            ).fetchone()[0]

            # "Active upstreams" = real (non-aggregate) accounts with a request
            # today. account_id is the identity; is_aggregate comes from a JOIN
            # (soft-deleted accounts are excluded — they are no longer routed).
            active_upstreams = conn.execute(
                "SELECT COUNT(DISTINCT r.account_id) FROM request_log r "
                "JOIN upstream_accounts a ON a.id = r.account_id "
                "WHERE COALESCE(a.is_aggregate, 0) = 0 "
                "  AND a.deleted_at IS NULL AND date(r.requested_at) = ? "
                "  AND r.account_id IS NOT NULL",
                (today,),
            ).fetchone()[0]

            total_accounts = conn.execute(
                "SELECT COUNT(*) FROM upstream_accounts "
                "WHERE is_aggregate = 0 AND deleted_at IS NULL"
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
                "max_concurrency, created_at, "
                "(SELECT COUNT(*) FROM upstream_keys k WHERE k.account_id = upstream_accounts.id) AS key_count "
                "FROM upstream_accounts WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
            accounts = []
            for r in rows:
                acc = dict(r)
                # Masked key list for display / edit-form placeholders (secrets
                # never leave the server in plaintext).  ids let the frontend
                # reference kept keys without ever sending the real values.
                key_rows = conn.execute(
                    "SELECT id, key_value FROM upstream_keys "
                    "WHERE account_id = ? ORDER BY position, id",
                    (acc["id"],),
                ).fetchall()
                acc["keys"] = [{"id": k[0], "masked": mask_key(k[1])}
                               for k in key_rows] if key_rows else []
                accounts.append(acc)
            return accounts
        finally:
            conn.close()

    @staticmethod
    def _normalize_keys(data: dict) -> list[str]:
        """Collect the intended upstream key list, dropping empty entries.

        Precedence: explicit `upstream_keys` list (new UI) → legacy single
        `upstream_key` scalar.  Empty strings are dropped (留空=保持 convention).
        """
        if data.get("upstream_keys") is not None:
            raw = data["upstream_keys"]
            if isinstance(raw, str):
                raw = [raw]
            return [k for k in raw if isinstance(k, str) and k.strip()]
        if data.get("upstream_key"):
            return [data["upstream_key"]]
        return []

    @staticmethod
    def _set_upstream_keys(conn: sqlite3.Connection, account_id: int,
                           keys: list[str]) -> None:
        """Replace an account's key set (position = list index)."""
        conn.execute("DELETE FROM upstream_keys WHERE account_id = ?", (account_id,))
        for i, k in enumerate(keys):
            conn.execute(
                "INSERT OR IGNORE INTO upstream_keys "
                "(account_id, key_value, position) VALUES (?,?,?)",
                (account_id, k, i),
            )

    @staticmethod
    def _plan_key_count(conn: sqlite3.Connection, account_id: int) -> int:
        """Number of keys configured for an account — at least 1 (an account
        with no upstream_keys rows falls back to its legacy single
        upstream_key slot, so its plan subscription still counts as 1)."""
        n = conn.execute(
            "SELECT COUNT(*) FROM upstream_keys WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0]
        return max(1, n)

    def create_account(self, data: dict) -> int:
        keys = self._normalize_keys(data)
        first = keys[0] if keys else ""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts "
                "(name, upstream_key, base_url, api_format, endpoint_path, auth_header, "
                " account_type, monthly_price, max_concurrency) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data["name"],
                    first,
                    data.get("base_url", ""),
                    data.get("api_format", "openai"),
                    data.get("endpoint_path", ""),
                    data.get("auth_header", "bearer"),
                    data.get("account_type", "api"),
                    float(data.get("monthly_price", 0) or 0),
                    data.get("max_concurrency"),
                ),
            )
            account_id = cursor.lastrowid
            if keys:
                self._set_upstream_keys(conn, account_id, keys)
            conn.commit()
            return account_id
        finally:
            conn.close()

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            fields = []
            values = []
            for key in ("name", "base_url", "api_format",
                        "endpoint_path", "auth_header", "account_type",
                        "monthly_price", "max_concurrency"):
                if key not in data:
                    continue
                if key == "monthly_price":
                    val = float(data[key] or 0)
                elif key == "max_concurrency":
                    val = data[key] if data[key] not in (None, "") else None
                else:
                    val = data[key]
                fields.append(f"{key} = ?")
                values.append(val)

            # Keys are only touched when the client explicitly edited them
            # (keys_edited=true) or sent a legacy non-empty `upstream_key`;
            # otherwise the existing key set is preserved (e.g. a rename-only
            # edit must never wipe the keys).
            replace_keys = bool(data.get("keys_edited")) or bool(data.get("upstream_key"))
            if replace_keys:
                # Replace semantics: the final key set = kept existing keys (by
                # id — the UI only ever sees masked values, never the real
                # secrets) + newly-typed keys, in that order.
                keep_ids = [int(i) for i in (data.get("keep_key_ids") or []) if str(i).lstrip('-').isdigit()]
                keep_vals = []
                if keep_ids:
                    ph = ",".join("?" for _ in keep_ids)
                    rows = conn.execute(
                        f"SELECT key_value FROM upstream_keys "
                        f"WHERE account_id = ? AND id IN ({ph}) "
                        f"ORDER BY position, id",
                        [account_id] + keep_ids,
                    ).fetchall()
                    keep_vals = [r[0] for r in rows]
                final_keys = keep_vals + self._normalize_keys(data)
                self._set_upstream_keys(conn, account_id, final_keys)
                first = final_keys[0] if final_keys else ""
                fields.append("upstream_key = ?")
                values.append(first)

            if not fields:
                conn.commit()
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

    def delete_account(self, account_id: int, mode: str = "detach") -> dict:
        """Soft-delete an account. Returns {ok: bool, error: str}.

        The row is kept (id is permanent, never recycled) and flagged with
        deleted_at. The account stops being routed and disappears from lists
        (all queries filter deleted_at IS NULL), but its historical
        request_log rows keep their account_id and the dashboard archive keeps
        showing the name (the accounts mirror preserves soft-deleted entries).
        request_log rows are NOT touched; they are cleaned 30 days after
        export by the normal high-water-mark cleanup.

        mode:
          "cascade" — also hard-delete this account's local keys.
          "detach"  — unbind the keys (account_id → NULL, keys stay for reuse).
        aggregate_entries referencing this account are deleted so an aggregate
        chain never routes to a dead account.
        """
        conn = self._connect()
        try:
            if mode == "cascade":
                conn.execute("DELETE FROM local_keys WHERE account_id = ?", (account_id,))
            else:
                conn.execute(
                    "UPDATE local_keys SET account_id = NULL WHERE account_id = ?",
                    (account_id,),
                )
            conn.execute("DELETE FROM aggregate_entries WHERE account_id = ?", (account_id,))
            conn.execute(
                "DELETE FROM aggregate_entries WHERE upstream_account_id = ?",
                (account_id,),
            )
            conn.execute(
                "UPDATE upstream_accounts SET deleted_at = datetime('now', 'localtime') "
                "WHERE id = ? AND deleted_at IS NULL",
                (account_id,),
            )
            conn.commit()
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
                "WHERE is_aggregate = 1 AND deleted_at IS NULL ORDER BY id"
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
            return True
        finally:
            conn.close()

    def delete_aggregate(self, agg_id: int) -> bool:
        """Soft-delete an aggregate account (id stays, row flagged deleted_at)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE upstream_accounts SET deleted_at = datetime('now', 'localtime') "
                "WHERE id=? AND is_aggregate=1 AND deleted_at IS NULL",
                (agg_id,),
            )
            conn.execute("DELETE FROM aggregate_entries WHERE account_id=?", (agg_id,))
            conn.commit()
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
        """Hard-delete a local key. Its request_log rows are kept and their
        local_key_id is set to NULL via the ON DELETE SET NULL foreign key,
        so usage/billing data is preserved."""
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
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_pricing(self, pricing_id: int) -> bool:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM model_pricing WHERE id = ?", (pricing_id,))
            conn.commit()
            # pricing_slots rows are removed by ON DELETE CASCADE.
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
                    COALESCE(SUM(r.api_cost), 0) AS cost
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND COALESCE(a.account_type, 'api') = 'api'
            """
            params = []
            if account_id:
                sql += " AND r.account_id = ?"
                params.append(account_id)
            if date_from:
                sql += " AND date(r.requested_at) >= ?"
                params.append(date_from)
            else:
                sql += " AND r.requested_at >= datetime('now', '-' || ? || ' days')"
                params.append(str(days))
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
                    r.id, r.account_id, COALESCE(a.name, 'unknown') AS account_name,
                    r.model, r.prompt_tokens, r.cache_read_tokens,
                    r.completion_tokens,
                    r.total_tokens, r.api_cost AS cost, r.is_streaming,
                    r.status_code, r.duration_ms, r.requested_at
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
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

    def get_daily_billing(self, days: int = 30) -> list[dict]:
        """Daily billing breakdown for the last *days* days (rolling window)."""
        conn = self._connect()
        try:
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(a.name, 'unknown') AS account_name,
                    COALESCE(SUM(r.api_cost), 0) AS cost,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND COALESCE(a.account_type, 'api') = 'api'
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at), r.account_id
                ORDER BY date, r.account_id
            """, (str(days),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_daily_billing_by_model(self, days: int = 30) -> list[dict]:
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
                    COALESCE(SUM(r.api_cost), 0) AS cost
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND COALESCE(a.account_type, 'api') = 'api'
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at)
                ORDER BY date
            """, (str(days),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

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
        """Per real upstream account used today: real/theoretical cost, tokens, requests.

        Active upstreams = non-aggregate accounts with at least one request
        today (UTC day, same boundary as get_stats); grouped by account_id (the
        identity), display name JOINed from upstream_accounts.
        """
        conn = self._connect()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT COALESCE(a.name, 'unknown') AS account_name,
                       COALESCE(SUM(CASE WHEN COALESCE(a.account_type, 'api') = 'api'
                                         THEN r.api_cost ELSE 0 END), 0) AS real_cost,
                       COALESCE(SUM(r.api_cost), 0) AS theoretical_cost,
                       COALESCE(SUM(r.total_tokens), 0) AS tokens,
                       COUNT(*) AS requests
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND date(r.requested_at) = ?
                GROUP BY r.account_id
                ORDER BY theoretical_cost DESC
            """, (today,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_to_dashboard(self, target_path: str, mark: int, max_id: int) -> dict:
        """Export request_log rows (id in (mark, max_id]) into a dashboard archive.

        Writes into *target_path* (the shadow archive built by sync_dashboard —
        cloud base + this machine's data) with additive upserts, exactly once per
        row. The high-water mark is advanced by the caller ONLY after the whole
        pull-export-upload transaction succeeds, so nothing here marks rows.

        plan subscription fees are persisted per (account, month): past months
        frozen forever, current month refreshed from the current monthly_price
        on every export regardless of the batch window (Requirement 4: price
        edits affect only the current month).
        """
        from app.dashboard_db import DashboardDatabase
        from app.migrations import schema_dir_for

        conn = self._connect()
        try:
            # The shadow may live outside data/ (e.g. data/tmp_dash/), so derive
            # the schema dir from the canonical sibling dashboard.db path.
            dash_schema_dir = schema_dir_for(
                os.path.join(os.path.dirname(self.db_path), "dashboard.db"), "dashboard"
            )
            dash_db = DashboardDatabase(target_path, schema_dir=dash_schema_dir)

            # A) usage + frozen cost: keyed by account_id (the identity). The
            #    display name comes from the dashboard `accounts` mirror.
            rows = conn.execute("""
                SELECT
                    date(r.requested_at) AS date,
                    r.account_id,
                    r.model,
                    COALESCE(SUM(r.prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(r.completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(r.cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(r.api_cost), 0) AS cost,
                    COUNT(*) AS request_count
                FROM request_log r
                WHERE r.id > ? AND r.id <= ?
                  AND LOWER(r.model) != 'unknown' AND r.model != ''
                  AND r.account_id IS NOT NULL
                GROUP BY date(r.requested_at), r.account_id, r.model
                ORDER BY date(r.requested_at), r.account_id, r.model
            """, (mark, max_id)).fetchall()

            dash_count = 0
            for r in rows:
                dash_count += dash_db.upsert_proxy_data(
                    date=r["date"],
                    model=r["model"],
                    account_id=r["account_id"],
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    cache_read_tokens=r["cache_read_tokens"],
                    request_count=r["request_count"],
                    cost=r["cost"],
                )

            # B) plan economics: persist per (account, month) — logs are cleaned
            #    30d after export, so the archive must never be recomputed.
            #    account_id is the identity; account_type JOINed (soft-deleted
            #    accounts still archive their usage — the accounts mirror keeps
            #    their name for display).
            current_month = datetime.now(timezone.utc).strftime("%Y-%m")
            plan_rows = conn.execute("""
                SELECT
                    r.account_id,
                    strftime('%Y-%m', r.requested_at) AS month,
                    COALESCE(SUM(r.api_cost), 0) AS virtual_cost
                FROM request_log r
                JOIN upstream_accounts a ON a.id = r.account_id
                WHERE r.id > ? AND r.id <= ?
                  AND COALESCE(a.account_type, 'api') = 'plan'
                GROUP BY r.account_id, month
            """, (mark, max_id)).fetchall()
            for pr in plan_rows:
                price = conn.execute(
                    "SELECT monthly_price, deleted_at FROM upstream_accounts WHERE id = ?",
                    (pr["account_id"],),
                ).fetchone()
                if price is not None:
                    dash_db.accumulate_plan_summary(
                        month=pr["month"],
                        account_id=pr["account_id"],
                        subscription_cost=float(price["monthly_price"] or 0)
                                        * self._plan_key_count(conn, pr["account_id"]),
                        virtual_cost=float(pr["virtual_cost"] or 0),
                        refresh_subscription=(pr["month"] == current_month
                                              and price["deleted_at"] is None),
                    )
                else:
                    # account gone: keep existing subscription_cost, just
                    # accumulate the virtual (api-billed) amount.
                    dash_db.accumulate_plan_summary(
                        month=pr["month"],
                        account_id=pr["account_id"],
                        subscription_cost=0.0,
                        virtual_cost=float(pr["virtual_cost"] or 0),
                        refresh_subscription=False,
                    )

            # Current-month refresh is NOT tied to the export batch: a price
            # edit mid-month must reach the archive even when this batch has no
            # new plan rows (all of the month's rows were already exported and
            # mark has passed them). Every ACTIVE plan account used this month
            # gets its subscription set to the current monthly_price. Past
            # months are never touched here (frozen in the batch loop above).
            cur_plans = conn.execute("""
                SELECT a.id, a.monthly_price
                FROM (
                    SELECT DISTINCT r.account_id AS id
                    FROM request_log r
                    WHERE strftime('%Y-%m', r.requested_at) = ?
                      AND r.account_id IS NOT NULL
                ) t
                JOIN upstream_accounts a ON a.id = t.id
                WHERE COALESCE(a.account_type, 'api') = 'plan'
                  AND a.deleted_at IS NULL
            """, (current_month,)).fetchall()
            for cp in cur_plans:
                dash_db.accumulate_plan_summary(
                    month=current_month,
                    account_id=cp["id"],
                    subscription_cost=float(cp["monthly_price"] or 0)
                                    * self._plan_key_count(conn, cp["id"]),
                    virtual_cost=0.0,
                    refresh_subscription=True,
                )

            return {
                "record_count": len(rows),
                "dashboard_records": dash_count,
            }
        finally:
            conn.close()

    # ── Request Log Cleanup ──────────────────────────────────────────

    def cleanup_exported_logs(self, mark: int, max_age_days: int = 30) -> int:
        """Delete archived request_log rows older than *max_age_days*.

        Only rows with id <= mark are deleted — those are the rows already
        counted into the archive (the high-water mark advances only after a
        successful upload). Rows with id > mark (never exported) are kept
        forever: a failed transaction never advances the mark, so nothing is
        ever lost.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM request_log "
                "WHERE id <= ? AND requested_at < datetime('now', '-' || ? || ' days')",
                (mark, str(max_age_days)),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_export_mark(self) -> int:
        """Read the sync high-water mark (last successfully exported log id)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'last_exported_log_id'"
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            conn.close()

    def set_export_mark(self, max_id: int):
        """Advance the high-water mark (commit point of a successful sync)."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) "
                "VALUES ('last_exported_log_id', ?)",
                (str(max_id),),
            )
            conn.commit()
        finally:
            conn.close()

    def get_max_log_id(self) -> int:
        """Current max request_log id (upper bound for the next export batch)."""
        conn = self._connect()
        try:
            return conn.execute("SELECT COALESCE(MAX(id), 0) FROM request_log").fetchone()[0]
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

        'Real upstream' = non-aggregate accounts (is_aggregate via JOIN).
        Success = status < 400. Sourced from request_log.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT COALESCE(a.name, 'unknown') AS account_name, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN r.status_code >= 400 THEN 1 ELSE 0 END) AS errors "
                "FROM request_log r "
                "LEFT JOIN upstream_accounts a ON a.id = r.account_id "
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

