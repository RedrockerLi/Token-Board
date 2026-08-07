"""Python-side SQLite access layer for the proxy tables.

Used by the Flask dashboard to manage upstream accounts, local API keys,
model pricing, and to read billing/usage data written by the C++ proxy.

Thread-safe: each method opens its own connection (SQLite in WAL mode
supports concurrent readers alongside a single writer).
"""

import json
import os
import secrets
import sqlite3
import string
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from app.domain.account_types import (
    ACCOUNT_TYPES,
    deletion_policy,
    holds_keys as type_holds_keys,
    import_types,
    is_routable,
    is_subscription,
    spec,
    sql_in,
    subscription_types,
    usage_billed_types,
)
from app.services.fx import ensure_rate as _fx_ensure_rate, rate_for_month as _fx_rate_for_month


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


UTC = timezone.utc


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_iso_date(value: object) -> date | None:
    """Parse a user-facing UTC calendar date, rejecting ambiguous values."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("订阅起始日必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("订阅起始日必须是 YYYY-MM-DD") from exc


def _parse_utc_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # SQLite's datetime('now') has a space separator and no offset.
        parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _period_start(month: str, anchor_day: int) -> datetime:
    year, month_number = (int(part) for part in month.split("-", 1))
    return datetime(year, month_number, min(anchor_day, monthrange(year, month_number)[1]), tzinfo=UTC)


def _previous_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-", 1))
    return f"{year - 1:04d}-12" if number == 1 else f"{year:04d}-{number - 1:02d}"


def _next_month(month: str) -> str:
    year, number = (int(part) for part in month.split("-", 1))
    return f"{year + 1:04d}-01" if number == 12 else f"{year:04d}-{number + 1:02d}"


def _billing_period_month(when: datetime | date, anchor_day: int) -> str:
    """Return the administrative billing month containing *when* (UTC)."""
    if isinstance(when, datetime):
        moment = when.astimezone(UTC)
        day = moment.date()
    else:
        day = when
    month = f"{day.year:04d}-{day.month:02d}"
    return month if day >= _period_start(month, anchor_day).date() else _previous_month(month)


def _iter_months(first: str, last: str):
    month = first
    while month <= last:
        yield month
        month = _next_month(month)


def _cancellation_end(config: sqlite3.Row, now: datetime, anchor_day: int,
                      account_type: str) -> datetime:
    """`deleted_at` a plan/agent key or account should receive on cancellation.

    api accounts are always terminated immediately (no subscription lifecycle).
    For subscription types the configured default deletion operation decides:
      'immediate'     → deleted_at = now (本期计费, 立即停止路由).
      'end_of_period' → deleted_at = end of the current billing period
                        (本期计费, 下期不计费); the entity keeps routing until
                        then because a future deleted_at is treated as active.
    `_billing_period_month(end, anchor_day)` must still equal the current
    period, hence the -1s before the next period's start.
    """
    if deletion_policy(account_type) == "immediate" or config["cancellation_mode"] == "immediate":
        return now
    current = _billing_period_month(now, anchor_day)
    return _period_start(_next_month(current), anchor_day) - timedelta(seconds=1)


class ProxyDatabase:
    """Manages the proxy SQLite database from the Flask side."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/proxy/*.sql); apply
        # once at construction. Fails fast (create_app aborts) on error.
        from app.db.migrations import migrate, schema_dir_for
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

        total_cost (real) = usage-billed accounts' billed usage in the last 30
        days (SUM(api_cost) where account_type is a usage-billed type;
        subscription accounts carry virtual only in api_cost) + current month's
        subscription fees. Plan subscription = monthly_price × key count per
        plan account used this month. Past months' fees live frozen in the
        archive and are NOT recomputed here.
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

            # Real billed usage: usage-billed accounts only (subscription
            # accounts' api_cost is their virtual/theoretical bill — never
            # counted as real, which would double-count their subscription).
            total_cost = conn.execute(
                f"SELECT COALESCE(SUM(r.api_cost), 0) FROM request_log r "
                "JOIN upstream_accounts a ON a.id = r.account_id "
                f"WHERE COALESCE(a.account_type, 'api') IN ({sql_in(usage_billed_types())}) "
                "  AND r.requested_at >= datetime('now', '-30 days')",
                usage_billed_types(),
            ).fetchone()[0]

            # Current plan/agent subscriptions are independent of usage.  Use the
            # same lifecycle/price-history resolver as dashboard export so a
            # deleted key remains billed for the current period it touched
            # (no grace window anymore; an end-of-period deletion simply ends
            # the lifecycle at this period's end).  Native (plan_price_history)
            # prices are in the account's currency; USD subscriptions are
            # converted to CNY at today's rate (fetch on demand; stale fallback
            # handled inside fx).
            plan_subscription = 0.0
            now = _utc_now()
            for meta in self._plan_key_billing_meta(conn):
                current_period = _billing_period_month(now, meta["anchor"].day)
                native = self._subscription_periods(conn, meta).get(current_period, 0.0)
                if native and meta.get("currency") == "USD":
                    native *= _fx_ensure_rate(conn)
                plan_subscription += native
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
            # (soft-deleted accounts are excluded — a future deleted_at from an
            # end-of-period cancellation is still routing, so it stays counted).
            active_upstreams = conn.execute(
                "SELECT COUNT(DISTINCT r.account_id) FROM request_log r "
                "JOIN upstream_accounts a ON a.id = r.account_id "
                "WHERE COALESCE(a.is_aggregate, 0) = 0 "
                "  AND (a.deleted_at IS NULL OR a.deleted_at > datetime('now')) "
                "  AND date(r.requested_at) = ? "
                "  AND r.account_id IS NOT NULL",
                (today,),
            ).fetchone()[0]

            total_accounts = conn.execute(
                "SELECT COUNT(*) FROM upstream_accounts "
                "WHERE is_aggregate = 0 "
                "  AND (deleted_at IS NULL OR deleted_at > datetime('now'))"
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
                "SELECT id, name, base_url, api_format, "
                "COALESCE(endpoint_path,'') AS endpoint_path, "
                "COALESCE(auth_header,'bearer') AS auth_header, "
                "COALESCE(is_aggregate,0) AS is_aggregate, "
                "COALESCE(account_type,'api') AS account_type, "
                "COALESCE((SELECT pph.monthly_price FROM plan_price_history pph "
                "          WHERE pph.account_id = upstream_accounts.id "
                "            AND pph.effective_mode = 'current_period' "
                "          ORDER BY pph.changed_at DESC, pph.id DESC LIMIT 1), 0) "
                "  AS monthly_price, "
                "COALESCE(currency,'CNY') AS currency, "
                "COALESCE(agent_kind,'') AS agent_kind, "
                "COALESCE(valid_from,'') AS valid_from, "
                "max_concurrency, created_at, deleted_at, "
                "(SELECT COUNT(*) FROM upstream_keys k WHERE k.account_id = upstream_accounts.id "
                " AND (k.deleted_at IS NULL OR k.deleted_at > datetime('now'))) AS key_count "
                "FROM upstream_accounts "
                "WHERE (deleted_at IS NULL OR deleted_at > datetime('now')) "
                "ORDER BY id"
            ).fetchall()
            accounts = []
            for r in rows:
                acc = dict(r)
                # Masked key list for display / edit-form placeholders (secrets
                # never leave the server in plaintext).  ids let the frontend
                # reference kept keys without ever sending the real values.
                key_rows = conn.execute(
                    "SELECT id, key_value, COALESCE(valid_from, date(created_at)) AS valid_from, "
                    "deleted_at "
                    "FROM upstream_keys WHERE account_id = ? "
                    "  AND (deleted_at IS NULL OR deleted_at > datetime('now')) "
                    "ORDER BY position, id",
                    (acc["id"],),
                ).fetchall()
                acc["keys"] = [{"id": k[0], "masked": mask_key(k[1]),
                                "valid_from": k[2], "deleted_at": k[3]}
                               for k in key_rows] if key_rows else []
                # cloud-only 密钥：云端镜像里有、本机没有明文，无法使用/路由/计费。
                # 前端展示输入框让用户补填明文（POST cloud-keys 确认后变成本地 key）。
                cloud_rows = conn.execute(
                    "SELECT key_masked, valid_from FROM upstream_keys_cloud "
                    "WHERE account_id = ?",
                    (acc["id"],),
                ).fetchall()
                local_masked = {k["masked"] for k in acc["keys"]}
                acc["cloud_keys"] = [
                    {"masked": r[0], "valid_from": r[1]}
                    for r in cloud_rows if r[0] not in local_masked
                ] if cloud_rows else []
                accounts.append(acc)
            return accounts
        finally:
            conn.close()

    @staticmethod
    def _normalize_keys(data: dict) -> list[str]:
        """Collect the intended upstream key list, dropping empty entries.

        `upstream_keys` is the only key source since the legacy single-column
        `upstream_key` was removed.  Empty strings are dropped (留空=保持).
        """
        raw = data.get("upstream_keys") or []
        if isinstance(raw, str):
            raw = [raw]
        return [k for k in raw if isinstance(k, str) and k.strip()]

    @staticmethod
    def _billing_config_conn(conn: sqlite3.Connection) -> sqlite3.Row:
        return conn.execute(
            "SELECT price_change_effective, cancellation_mode "
            "FROM plan_billing_config WHERE id=1"
        ).fetchone()

    @staticmethod
    def _refresh_upstream_keys_cloud(conn: sqlite3.Connection, account_id: int) -> None:
        """Mirror local key lifecycle metadata without ever copying a secret."""
        conn.execute("DELETE FROM upstream_keys_cloud WHERE account_id=?", (account_id,))
        for row in conn.execute(
            "SELECT key_value, position, valid_from, deleted_at "
            "FROM upstream_keys WHERE account_id=?", (account_id,)
        ):
            conn.execute(
                "INSERT INTO upstream_keys_cloud "
                "(account_id,key_masked,position,valid_from,deleted_at) "
                "VALUES (?,?,?,?,?)",
                (account_id, mask_key(row["key_value"]), row["position"],
                 row["valid_from"], row["deleted_at"]),
            )

    @staticmethod
    def _set_upstream_keys(conn: sqlite3.Connection, account_id: int,
                           keep_ids: list[int], new_keys: list[str],
                           keep_valid_froms: dict[str, object] | None = None,
                           new_valid_froms: list[object] | None = None,
                           account_type: str = "api") -> list[str]:
        """Diff an account's local keys, preserving soft-deleted lifecycles.

        Returns active plaintext values only so the legacy account column can
        retain its first active fallback key.  This function is called inside
        the same transaction that updates the cloud-safe metadata mirror.

        Removed keys get `deleted_at` per the account type's deletion policy
        (see _cancellation_end): usage-billed keys stop immediately;
        subscription keys either stop now ('immediate') or at the end of their
        current billing period ('end_of_period').  An end-of-period key keeps
        routing until then, so the "active" set includes future-deleted keys.
        """
        keep_valid_froms = keep_valid_froms or {}
        new_valid_froms = new_valid_froms or []
        active = {
            row["id"]: row for row in conn.execute(
                "SELECT id,key_value,valid_from,created_at FROM upstream_keys "
                "WHERE account_id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                (account_id,)
            )
        }
        retained = [key_id for key_id in keep_ids if key_id in active]
        config = ProxyDatabase._billing_config_conn(conn)
        now = _utc_now()
        for key_id in active:
            if key_id not in retained:
                row = active[key_id]
                anchor = _parse_iso_date(row["valid_from"]) or _parse_utc_timestamp(
                    row["created_at"]
                ).date()
                end = _cancellation_end(config, now, anchor.day, account_type)
                conn.execute(
                    "UPDATE upstream_keys SET deleted_at=? WHERE id=?",
                    (end.strftime("%Y-%m-%d %H:%M:%S"), key_id),
                )

        active_values: list[str] = []
        position = 0
        for key_id in retained:
            valid = _parse_iso_date(keep_valid_froms.get(str(key_id)))
            conn.execute(
                "UPDATE upstream_keys SET position=?, valid_from=? WHERE id=?",
                (position, valid.isoformat() if valid else None, key_id),
            )
            active_values.append(active[key_id]["key_value"])
            position += 1

        seen = set(active_values)
        for index, raw_key in enumerate(new_keys):
            key = raw_key.strip()
            if not key or key in seen:
                continue
            valid = _parse_iso_date(new_valid_froms[index] if index < len(new_valid_froms) else None)
            conn.execute(
                "INSERT INTO upstream_keys (account_id,key_value,position,valid_from) "
                "VALUES (?,?,?,?)",
                (account_id, key, position, valid.isoformat() if valid else None),
            )
            active_values.append(key)
            seen.add(key)
            position += 1

        ProxyDatabase._refresh_upstream_keys_cloud(conn, account_id)
        return active_values

    def confirm_cloud_key(self, account_id: int, masked: str, key_value: str) -> bool:
        """把 cloud-only 密钥补填明文，变成这台机器的本地 key。

        校验该打码 identity 确在云端镜像、且本机还没有这把 key 的明文后，写入
        ``upstream_keys`` 并刷新云端镜像——此后它正常路由 / 计费。返回 False 表示
        云端没有该记录。
        """
        key_value = (key_value or "").strip()
        if not key_value:
            raise ValueError("请输入密钥明文")
        conn = self._connect()
        try:
            cloud = conn.execute(
                "SELECT valid_from FROM upstream_keys_cloud "
                "WHERE account_id=? AND key_masked=?",
                (account_id, masked),
            ).fetchone()
            if cloud is None:
                return False
            existing = conn.execute(
                "SELECT 1 FROM upstream_keys WHERE account_id=? AND key_value=?",
                (account_id, key_value),
            ).fetchone()
            if existing is not None:
                # 明文已在本机存在 → 无需重复插入，刷新镜像即可。
                ProxyDatabase._refresh_upstream_keys_cloud(conn, account_id)
                conn.commit()
                return True
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM upstream_keys "
                "WHERE account_id=?",
                (account_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO upstream_keys "
                "(account_id, key_value, position, valid_from) VALUES (?,?,?,?)",
                (account_id, key_value, position, cloud["valid_from"]),
            )
            ProxyDatabase._refresh_upstream_keys_cloud(conn, account_id)
            conn.commit()
            return True
        finally:
            conn.close()

    def create_account(self, data: dict) -> int:
        account_type = data.get("account_type", "api")
        if account_type not in ACCOUNT_TYPES:
            raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
        currency = data.get("currency", "CNY")
        if currency not in ("CNY", "USD"):
            raise ValueError("币种必须是 CNY / USD")
        keys = self._normalize_keys(data)
        type_spec = spec(account_type)
        valid_from = _parse_iso_date(data.get("valid_from"))
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO upstream_accounts "
                "(name, base_url, api_format, endpoint_path, auth_header, "
                " account_type, currency, max_concurrency, valid_from) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    data["name"],
                    data.get("base_url", ""),
                    data.get("api_format", "openai"),
                    data.get("endpoint_path", ""),
                    data.get("auth_header", "bearer"),
                    account_type,
                    currency,
                    data.get("max_concurrency"),
                    valid_from.isoformat() if valid_from else None,
                ),
            )
            account_id = cursor.lastrowid
            if type_spec.usage_source == "import" and data.get("agent_kind"):
                conn.execute(
                    "UPDATE upstream_accounts SET agent_kind=? WHERE id=?",
                    (str(data["agent_kind"]), account_id),
                )
            # Import-driven types are subscription-only: never create keys.
            if keys and type_spec.holds_keys:
                self._set_upstream_keys(
                    conn, account_id, [], keys, new_valid_froms=data.get("new_valid_froms"),
                    account_type=account_type,
                )
            # Subscription-billed types seed the baseline price history so
            # _subscription_periods has a deterministic value.
            if is_subscription(account_type):
                conn.execute(
                    "INSERT INTO plan_price_history "
                    "(account_id,monthly_price,changed_at,effective_mode) VALUES (?,?,?,?)",
                    (account_id, float(data.get("monthly_price", 0) or 0),
                     _utc_now().strftime("%Y-%m-%d %H:%M:%S"), "current_period"),
                )
            conn.commit()
            return account_id
        finally:
            conn.close()

    def update_account(self, account_id: int, data: dict) -> bool:
        conn = self._connect()
        try:
            original = conn.execute(
                "SELECT account_type, currency, "
                "COALESCE((SELECT pph.monthly_price FROM plan_price_history pph "
                "          WHERE pph.account_id = upstream_accounts.id "
                "            AND pph.effective_mode = 'current_period' "
                "          ORDER BY pph.changed_at DESC, pph.id DESC LIMIT 1), 0) "
                "  AS current_price "
                "FROM upstream_accounts "
                "WHERE id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                (account_id,),
            ).fetchone()
            if original is None:
                return False
            fields = []
            values = []
            for key in ("name", "base_url", "api_format",
                        "endpoint_path", "auth_header", "account_type",
                        "currency", "agent_kind",
                        "max_concurrency", "valid_from"):
                if key not in data:
                    continue
                if key == "max_concurrency":
                    val = data[key] if data[key] not in (None, "") else None
                elif key == "valid_from":
                    parsed = _parse_iso_date(data[key])
                    val = parsed.isoformat() if parsed else None
                elif key == "currency":
                    val = data[key]
                    if val not in ("CNY", "USD"):
                        raise ValueError("币种必须是 CNY / USD")
                elif key == "account_type":
                    val = data[key]
                    if val not in ACCOUNT_TYPES:
                        raise ValueError("账户类型必须是 " + " / ".join(ACCOUNT_TYPES))
                else:
                    val = data[key]
                fields.append(f"{key} = ?")
                values.append(val)

            # Keys are only touched when the client explicitly edited them
            # (keys_edited=true); otherwise the existing key set is preserved
            # (e.g. a rename-only edit must never wipe the keys).  Types that
            # don't hold upstream keys (e.g. agent) never get key replacement.
            replace_keys = bool(data.get("keys_edited")) \
                and type_holds_keys(original["account_type"])
            if replace_keys:
                # Replace semantics: the final key set = kept existing keys (by
                # id — the UI only ever sees masked values, never the real
                # secrets) + newly-typed keys, in that order.
                keep_ids = [int(i) for i in (data.get("keep_key_ids") or []) if str(i).lstrip('-').isdigit()]
                self._set_upstream_keys(
                    conn, account_id, keep_ids, self._normalize_keys(data),
                    keep_valid_froms=data.get("keep_valid_froms"),
                    new_valid_froms=data.get("new_valid_froms"),
                    account_type=original["account_type"],
                )

            # `monthly_price` is no longer an upstream_accounts column (single
            # price source is plan_price_history); a price-only edit must still
            # reach the history write below, so it is not part of `fields`.
            price_requested = "monthly_price" in data
            if not fields and not price_requested:
                conn.commit()
                return False
            if fields:
                values.append(account_id)
                conn.execute(
                    f"UPDATE upstream_accounts SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
            final_type = data.get("account_type", original["account_type"])
            final_price = float(data.get("monthly_price", original["current_price"]) or 0)
            price_changed = price_requested and \
                final_price != float(original["current_price"] or 0)
            if is_subscription(final_type) and (
                original["account_type"] != final_type or price_changed
            ):
                mode = data.get("price_effective")
                if mode is not None and mode not in ("current_period", "next_period"):
                    raise ValueError("价格生效方式必须是 current_period 或 next_period")
                if mode is None:
                    mode = self._billing_config_conn(conn)["price_change_effective"]
                conn.execute(
                    "INSERT INTO plan_price_history "
                    "(account_id,monthly_price,changed_at,effective_mode) VALUES (?,?,?,?)",
                    (account_id, final_price, _utc_now().strftime("%Y-%m-%d %H:%M:%S"), mode),
                )
            self._refresh_upstream_keys_cloud(conn, account_id)
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def delete_account(self, account_id: int, mode: str = "detach") -> dict:
        """Soft-delete an account. Returns {ok: bool, error: str}.

        The row is kept (id is permanent, never recycled) and flagged with
        deleted_at. The account stops being routed and disappears from lists
        (queries treat a past deleted_at as gone), but its historical
        request_log rows keep their account_id and the dashboard archive keeps
        showing the name (the accounts mirror preserves soft-deleted entries).
        request_log rows are NOT touched; they are cleaned 30 days after
        export by the normal high-water-mark cleanup.

        mode:
          "cascade" — also delete this account's local keys.
          "detach"  — unbind the keys (account_id → NULL, keys stay for reuse).
        aggregate_entries referencing this account are deleted so an aggregate
        chain never routes to a dead account.

        api accounts are always terminated immediately.  subscription accounts
        follow the configured default deletion operation:
          'immediate'     — deleted_at = now; local keys & aggregates cleaned up
                            right here.
          'end_of_period' — deleted_at = end of each key's current billing
                            period (the account keeps routing until then; local
                            keys & aggregates are kept so clients can still
                            reach it).  The cleanup intent is recorded in
                            deferred_cleanup_mode and performed by the deletion
                            finalizer once deleted_at has passed.
        """
        conn = self._connect()
        try:
            config = self._billing_config_conn(conn)
            account = conn.execute(
                "SELECT account_type, created_at, valid_from FROM upstream_accounts "
                "WHERE id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                (account_id,),
            ).fetchone()
            if account is None:
                return {"ok": False, "error": "Account not found"}
            account_type = account["account_type"] or "api"
            cancelled_at = _utc_now()
            account_anchor = (_parse_iso_date(account["valid_from"])
                              or _parse_utc_timestamp(account["created_at"]).date())
            active_keys = conn.execute(
                "SELECT id, valid_from, created_at FROM upstream_keys "
                "WHERE account_id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                (account_id,),
            ).fetchall()
            end_of_period = deletion_policy(account_type) == "configurable" \
                and config["cancellation_mode"] == "end_of_period"

            if end_of_period:
                # Keep routing until the period end; defer the local-key and
                # aggregate cleanup to the deletion finalizer.
                key_ends = []
                for key in active_keys:
                    anchor = _parse_iso_date(key["valid_from"]) or _parse_utc_timestamp(
                        key["created_at"]
                    ).date()
                    key_ends.append(_cancellation_end(config, cancelled_at, anchor.day,
                                                      account_type))
                account_end = max(key_ends) if key_ends else _cancellation_end(
                    config, cancelled_at, account_anchor.day, account_type
                )
                effective = account_end
                conn.execute(
                    "UPDATE upstream_accounts SET deleted_at=?, deferred_cleanup_mode=? "
                    "WHERE id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                    (account_end.strftime("%Y-%m-%d %H:%M:%S"), mode, account_id),
                )
                for key, end in zip(active_keys, key_ends):
                    conn.execute(
                        "UPDATE upstream_keys SET deleted_at=? WHERE id=?",
                        (end.strftime("%Y-%m-%d %H:%M:%S"), key["id"]),
                    )
            else:
                # Immediate: clean up local keys and aggregates right now.
                effective = cancelled_at
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
                    "UPDATE upstream_accounts SET deleted_at=?, deferred_cleanup_mode=NULL "
                    "WHERE id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                    (cancelled_at.strftime("%Y-%m-%d %H:%M:%S"), account_id),
                )
                conn.execute(
                    "UPDATE upstream_keys SET deleted_at=? "
                    "WHERE account_id=? AND (deleted_at IS NULL OR deleted_at > datetime('now'))",
                    (cancelled_at.strftime("%Y-%m-%d %H:%M:%S"), account_id),
                )
            self._refresh_upstream_keys_cloud(conn, account_id)
            conn.commit()
            return {"ok": conn.total_changes > 0, "error": "",
                    "cancellation_mode": config["cancellation_mode"],
                    "cancelled_at": cancelled_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "effective_deleted_at": effective.strftime("%Y-%m-%d %H:%M:%S"),
                    "deferred": effective > cancelled_at}
        finally:
            conn.close()

    def finalize_deferred_deletions(self) -> int:
        """Complete end-of-period account deletions whose time has come.

        Routing already stopped at deleted_at (queries treat a past
        deleted_at as gone); this only finishes the cleanup that was deferred
        at delete time — detach/cascade the local keys and drop aggregate
        references — and clears the marker.  Idempotent, safe to call on every
        sweep.
        """
        conn = self._connect()
        try:
            now = _utc_now().strftime("%Y-%m-%d %H:%M:%S")
            pending = conn.execute(
                "SELECT id, deferred_cleanup_mode FROM upstream_accounts "
                "WHERE deleted_at IS NOT NULL AND deleted_at <= ? "
                "  AND deferred_cleanup_mode IS NOT NULL",
                (now,),
            ).fetchall()
            for row in pending:
                account_id = row["id"]
                if row["deferred_cleanup_mode"] == "cascade":
                    conn.execute("DELETE FROM local_keys WHERE account_id = ?", (account_id,))
                else:
                    conn.execute(
                        "UPDATE local_keys SET account_id = NULL WHERE account_id = ?",
                        (account_id,),
                    )
                conn.execute(
                    "DELETE FROM aggregate_entries WHERE account_id = ?", (account_id,)
                )
                conn.execute(
                    "DELETE FROM aggregate_entries WHERE upstream_account_id = ?",
                    (account_id,),
                )
                conn.execute(
                    "UPDATE upstream_accounts SET deferred_cleanup_mode = NULL WHERE id = ?",
                    (account_id,),
                )
            conn.commit()
            return len(pending)
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

    def get_plain_keys(self, account_id: int) -> list[str]:
        """Plaintext upstream keys of an account (server-side only — never
        sent to the client; used by the concurrency-test route to hit the
        upstream directly)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT key_value FROM upstream_keys WHERE account_id=? "
                "AND (deleted_at IS NULL OR deleted_at > datetime('now')) "
                "ORDER BY position, id",
                (account_id,),
            ).fetchall()
            return [r[0] for r in rows if r[0]]
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
                "(name, base_url, api_format, is_aggregate) "
                "VALUES (?, '', 'openai', 1)",
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

    @staticmethod
    def _assert_routable_account(conn: sqlite3.Connection, account_id):
        """Reject binding a local key to a non-routable account.

        Non-routable types (e.g. agent / Codex) are subscription-only: they
        must never be used as a routable upstream, so no local key may point
        at one.
        """
        if account_id is None:
            return
        row = conn.execute(
            "SELECT COALESCE(account_type,'api') AS account_type FROM upstream_accounts "
            "WHERE id=?", (account_id,)
        ).fetchone()
        if row is not None and not is_routable(row["account_type"]):
            raise ValueError("Agent 账户不能作为本地密钥的上游")

    def create_key(self, data: dict) -> str:
        """Create a new local key. Returns the generated key value."""
        key_value = _generate_key()
        conn = self._connect()
        try:
            self._assert_routable_account(conn, data.get("account_id"))
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
            if "account_id" in data:
                self._assert_routable_account(conn, data["account_id"])
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

    def get_agent_accounts(self) -> list[dict]:
        """Agent (subscription-only, import-driven) accounts, e.g. Codex.

        Never routable.  The import-driven type set comes from the spec so a
        future import-driven type flows through automatically.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, COALESCE(agent_kind,'') AS agent_kind, "
                "COALESCE(currency,'CNY') AS currency, "
                "COALESCE((SELECT pph.monthly_price FROM plan_price_history pph "
                "          WHERE pph.account_id = upstream_accounts.id "
                "            AND pph.effective_mode = 'current_period' "
                "          ORDER BY pph.changed_at DESC, pph.id DESC LIMIT 1), 0) "
                "  AS monthly_price "
                "FROM upstream_accounts "
                f"WHERE COALESCE(account_type,'api') IN ({sql_in(import_types())}) "
                "  AND COALESCE(is_aggregate,0)=0 "
                "  AND (deleted_at IS NULL OR deleted_at > datetime('now')) "
                "ORDER BY id",
                import_types(),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _insert_agent_usage_row(conn, account_id: int, model: str,
                                prompt_tokens: int, completion_tokens: int,
                                cache_read_tokens: int, total_tokens: int,
                                requested_at: str, event_id: str) -> bool:
        """Insert one agent (Codex) usage row on the caller's connection.

        cost_frozen=0 so tr_request_log_insert computes api_cost (including USD
        FX conversion).  event_id is UNIQUE → INSERT OR IGNORE makes the import
        idempotent across crashes/restarts.  `requested_at` must be a SQLite UTC
        timestamp "YYYY-MM-DD HH:MM:SS".  Returns True when a row was inserted.
        """
        cur = conn.execute(
            "INSERT OR IGNORE INTO request_log "
            "(account_id, local_key_id, model, prompt_tokens, completion_tokens, "
            " cache_read_tokens, total_tokens, api_cost, is_streaming, status_code, "
            " duration_ms, upstream_key_id, requested_at, event_id, cost_frozen) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, 0, 0, 200, 0, NULL, ?, ?, 0)",
            (account_id, model, int(prompt_tokens), int(completion_tokens),
             int(cache_read_tokens), int(total_tokens), requested_at, event_id),
        )
        return cur.rowcount > 0

    def insert_agent_usage(self, account_id: int, model: str,
                           prompt_tokens: int, completion_tokens: int,
                           cache_read_tokens: int, total_tokens: int,
                           requested_at: str, event_id: str) -> bool:
        """Insert one imported agent (Codex) usage row into request_log.

        Convenience wrapper opening its own connection (for manual/API use);
        the background importer calls :meth:`_insert_agent_usage_row` on a
        shared connection instead to avoid write-lock contention.
        """
        conn = self._connect()
        try:
            ok = self._insert_agent_usage_row(
                conn, account_id, model, prompt_tokens, completion_tokens,
                cache_read_tokens, total_tokens, requested_at, event_id)
            conn.commit()
            return ok
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
            currency = data.get("currency", "CNY")
            if currency not in ("CNY", "USD"):
                raise ValueError("币种必须是 CNY / USD")
            cursor = conn.execute(
                "INSERT INTO model_pricing (model_pattern, input_price, output_price, cache_read_price, currency) "
                "VALUES (?, ?, ?, ?, ?)",
                (data["model_pattern"], data["input_price"], data["output_price"],
                 data.get("cache_read_price"), currency),
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
            for key in ("model_pattern", "input_price", "output_price",
                        "cache_read_price", "currency"):
                if key in data:
                    if key == "currency" and data[key] not in ("CNY", "USD"):
                        raise ValueError("币种必须是 CNY / USD")
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

    # ── Plan billing settings (all timestamps are UTC+0) ───────────────

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
            conn.execute(
                "UPDATE plan_billing_config SET price_change_effective=?, "
                "cancellation_mode=? WHERE id=1",
                (mode, cancellation),
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
            usage_billed = sql_in(usage_billed_types())
            sql = f"""
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
                  AND COALESCE(a.account_type, 'api') IN ({usage_billed})
            """
            params = list(usage_billed_types())
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
        include_attempts: bool = True,
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
                    r.status_code, r.ttft_ms, r.generation_ms, r.output_tps,
                    r.upstream_ttft_ms, r.upstream_duration_ms,
                    r.attempt_count, r.fallback_count,
                    r.requested_at
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
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
                               t.ttft_ms, t.is_timeout, t.error
                        FROM request_attempts t
                        LEFT JOIN upstream_accounts a ON a.id = t.account_id
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
            }
            conn.commit()
            return result
        finally:
            conn.close()

    def get_daily_billing(self, days: int = 30) -> list[dict]:
        """Daily billing breakdown for the last *days* days (rolling window)."""
        conn = self._connect()
        try:
            usage_billed = sql_in(usage_billed_types())
            rows = conn.execute(f"""
                SELECT
                    date(r.requested_at) AS date,
                    COALESCE(a.name, 'unknown') AS account_name,
                    COALESCE(SUM(r.api_cost), 0) AS cost,
                    COUNT(*) AS requests,
                    COALESCE(SUM(r.total_tokens), 0) AS total_tokens
                FROM request_log r
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE COALESCE(a.is_aggregate, 0) = 0
                  AND COALESCE(a.account_type, 'api') IN ({usage_billed})
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at), r.account_id
                ORDER BY date, r.account_id
            """, (*usage_billed_types(), str(days))).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_daily_billing_by_model(self, days: int = 30) -> list[dict]:
        """Daily billing breakdown with input/output token split (for stacked bar chart)."""
        conn = self._connect()
        try:
            usage_billed = sql_in(usage_billed_types())
            rows = conn.execute(f"""
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
                  AND COALESCE(a.account_type, 'api') IN ({usage_billed})
                  AND r.requested_at >= datetime('now', '-' || ? || ' days')
                GROUP BY date(r.requested_at)
                ORDER BY date
            """, (*usage_billed_types(), str(days))).fetchall()
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
            usage_billed = sql_in(usage_billed_types())
            rows = conn.execute(f"""
                SELECT COALESCE(a.name, 'unknown') AS account_name,
                       COALESCE(SUM(CASE WHEN COALESCE(a.account_type, 'api') IN ({usage_billed})
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
            """, (*usage_billed_types(), today)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Per-key plan billing helpers ───────────────────────────────────

    @staticmethod
    def _plan_key_billing_meta(conn: sqlite3.Connection) -> list[dict]:
        """Build billable key lifecycles, including cloud-only masked keys."""
        now = _utc_now()
        by_identity: dict[tuple[int, str], dict] = {}
        sub_types = subscription_types()
        accounts = conn.execute(
            "SELECT id, created_at, valid_from, deleted_at, "
            "COALESCE(account_type,'api') AS account_type, "
            "COALESCE(currency,'CNY') AS currency "
            "FROM upstream_accounts "
            f"WHERE COALESCE(account_type,'api') IN ({sql_in(sub_types)}) "
            "  AND COALESCE(is_aggregate,0)=0",
            sub_types,
        ).fetchall()
        account_rows = {row["id"]: row for row in accounts}
        for row in conn.execute(
            "SELECT k.id,k.account_id,k.key_value,k.created_at,k.valid_from,k.deleted_at,"
            "a.deleted_at AS account_deleted_at "
            "FROM upstream_keys k JOIN upstream_accounts a ON a.id=k.account_id "
            f"WHERE COALESCE(a.account_type,'api') IN ({sql_in(sub_types)}) "
            "  AND COALESCE(a.is_aggregate,0)=0",
            sub_types,
        ):
            anchor = _parse_iso_date(row["valid_from"]) or _parse_utc_timestamp(row["created_at"]).date()
            key_end = _parse_utc_timestamp(row["deleted_at"])
            account_end = _parse_utc_timestamp(row["account_deleted_at"])
            end = min((v for v in (key_end, account_end) if v is not None), default=None)
            identity = (row["account_id"], mask_key(row["key_value"]))
            by_identity[identity] = {
                "account_id": row["account_id"], "key_id": row["id"],
                "key_masked": identity[1], "anchor": anchor,
                "end": end,
            }
        for row in conn.execute("SELECT * FROM upstream_keys_cloud"):
            if row["account_id"] not in account_rows:
                continue
            identity = (row["account_id"], row["key_masked"])
            if identity not in by_identity:
                # cloud-only 密钥：本机没有该 key 的明文，无法使用/路由 →
                # 不计费。等用户在本机补填明文、变成本地 key 后才进入计费。
                continue
            meta = by_identity[identity]
            # 每把密钥独立计费：锚点永远是该 key 自己的 valid_from → 自己的
            # created_at（本地 loop 已算好）。云端镜像只合并删除边界，让跨
            # 机器的删除生效；绝不改本地 key 的锚点。
            cloud_end = _parse_utc_timestamp(row["deleted_at"])
            account_end = _parse_utc_timestamp(account_rows[row["account_id"]]["deleted_at"])
            end = min((v for v in (cloud_end, account_end) if v is not None), default=None)
            if end is not None:
                meta["end"] = min((v for v in (meta["end"], end) if v is not None), default=end)
        # Per-account subscription types (agent): no upstream keys exist, so
        # synthesize exactly one lifecycle per account, keyed "subscription".
        for account in accounts:
            if spec(account["account_type"]).subscription_unit != "per_account":
                continue
            anchor = (_parse_iso_date(account["valid_from"])
                      or _parse_utc_timestamp(account["created_at"]).date())
            by_identity[(account["id"], "subscription")] = {
                "account_id": account["id"], "key_id": None,
                "key_masked": "subscription", "anchor": anchor,
                "end": _parse_utc_timestamp(account["deleted_at"]),
                "currency": account["currency"] or "CNY",
                "account_type": account["account_type"],
            }
        for meta in by_identity.values():
            # Attach the account's native currency for FX conversion.  The
            # account_type default mirrors spec()/routing (api), not a legacy
            # billing assumption — an empty type must not be treated as plan.
            account_row = account_rows.get(meta["account_id"])
            meta["currency"] = (account_row["currency"] or "CNY") if account_row else "CNY"
            meta["account_type"] = (
                (account_row["account_type"] or spec("").account_type)
                if account_row else spec("").account_type
            )
            meta["now"] = now
        return list(by_identity.values())

    @staticmethod
    def _price_for_period(conn: sqlite3.Connection, account_id: int,
                          period: str, anchor_day: int) -> float:
        price = 0.0
        for row in conn.execute(
            "SELECT monthly_price,changed_at,effective_mode FROM plan_price_history "
            "WHERE account_id=? ORDER BY changed_at,id", (account_id,)
        ):
            changed = _parse_utc_timestamp(row["changed_at"])
            effective = _billing_period_month(changed, anchor_day)
            if row["effective_mode"] == "next_period":
                effective = _next_month(effective)
            if effective <= period:
                price = float(row["monthly_price"] or 0)
        return price

    @classmethod
    def _subscription_periods(cls, conn: sqlite3.Connection, meta: dict) -> dict[str, float]:
        now = meta["now"]
        first = _billing_period_month(meta["anchor"], meta["anchor"].day)
        last = _billing_period_month(meta["end"] or now, meta["anchor"].day)
        if first > last:
            return {}
        # Every admin month the lifecycle touches is billed in full — there is
        # no grace free-window anymore (uniform new rule, see 0015).
        return {month: cls._price_for_period(conn, meta["account_id"], month,
                                             meta["anchor"].day)
                for month in _iter_months(first, last)}

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
        from app.db.dashboard_db import DashboardDatabase
        from app.db.migrations import schema_dir_for

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
                LEFT JOIN upstream_accounts a ON a.id = r.account_id
                WHERE r.id > ? AND r.id <= ?
                  AND LOWER(r.model) != 'unknown' AND r.model != ''
                  AND r.account_id IS NOT NULL
                  -- Aggregate accounts are routing groupings, not real upstreams:
                  -- a request they fail to serve is logged against the aggregate
                  -- (zero tokens), which must never pollute the usage archive.
                  AND COALESCE(a.is_aggregate, 0) = 0
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

            # B) Plan/agent subscriptions are derived from key lifecycles, not
            # from usage.  Reconcile every known lifecycle on every export so an
            # edited start date, cancellation, or scheduled price cannot leave
            # stale subscription rows behind.  Native prices are converted to
            # CNY per month (past months frozen, current month at today's rate).
            metas = self._plan_key_billing_meta(conn)
            by_key_id = {meta["key_id"]: meta for meta in metas if meta["key_id"] is not None}
            by_account = {}
            for meta in metas:
                by_account.setdefault(meta["account_id"], meta)
                native_periods = self._subscription_periods(conn, meta)
                if meta.get("currency") == "USD":
                    native_periods = {
                        month: native * _fx_rate_for_month(conn, month)
                        for month, native in native_periods.items()
                    }
                dash_db.reconcile_plan_subscription(
                    meta["account_id"], meta["key_masked"],
                    native_periods,
                )

            virtual_buckets: dict[tuple[str, int, str], float] = {}
            sub_types = subscription_types()
            plan_logs = conn.execute(
                "SELECT r.account_id,r.upstream_key_id,r.requested_at,r.api_cost "
                "FROM request_log r JOIN upstream_accounts a ON a.id=r.account_id "
                f"WHERE r.id>? AND r.id<=? "
                f"  AND COALESCE(a.account_type,'api') IN ({sql_in(sub_types)})",
                (mark, max_id, *sub_types),
            ).fetchall()
            for log in plan_logs:
                meta = by_key_id.get(log["upstream_key_id"]) or by_account.get(log["account_id"])
                if meta is None:
                    continue
                requested = _parse_utc_timestamp(log["requested_at"])
                month = _billing_period_month(requested, meta["anchor"].day)
                bucket = (month, meta["account_id"], meta["key_masked"])
                virtual_buckets[bucket] = virtual_buckets.get(bucket, 0.0) + float(log["api_cost"] or 0)
            for (month, account_id, key_masked), virtual_cost in virtual_buckets.items():
                dash_db.accumulate_plan_summary(
                    month=month, account_id=account_id, key_masked=key_masked,
                    subscription_cost=0.0, virtual_cost=virtual_cost,
                    refresh_subscription=False,
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
            total, successes, tokens, avg_ttft = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(total_tokens), 0), "
                "AVG(CASE WHEN status_code BETWEEN 200 AND 299 "
                "              AND ttft_ms IS NOT NULL THEN ttft_ms END) "
                "FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes')",
                (str(window_minutes),),
            ).fetchone()
            successes = successes or 0
            errors = total - successes

            return {
                "total_requests": total,
                "error_count": errors,
                "success_rate": round(successes / max(total, 1) * 100, 1),
                "total_tokens": tokens,
                "avg_ttft_ms": round(avg_ttft, 1) if avg_ttft is not None else None,
            }
        finally:
            conn.close()

    def get_perf_latency(self, window_minutes: int = 60) -> list[dict]:
        """Observed streaming TTFT percentiles per bucket."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket, "
                "ttft_ms FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  AND status_code BETWEEN 200 AND 299 "
                "  AND ttft_ms IS NOT NULL "
                "ORDER BY bucket, ttft_ms",
                (str(window_minutes),),
            ).fetchall()
            result = []
            by_bucket = {}
            for bucket, ttft_ms in rows:
                by_bucket.setdefault(bucket, []).append(ttft_ms)
            for bucket, vals in by_bucket.items():
                if not vals:
                    continue
                n = len(vals)

                def percentile(p):
                    # Nearest-rank percentile: ceil(p*n/100)-1.
                    k = max(0, min(n - 1, (p * n + 99) // 100 - 1))
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

    def get_perf_speed(self, window_minutes: int = 60) -> list[dict]:
        """Observed streaming output-speed (tokens/s) percentiles per bucket."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT strftime('%Y-%m-%d %H:%M', requested_at) AS bucket, "
                "output_tps FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  AND status_code BETWEEN 200 AND 299 "
                "  AND output_tps IS NOT NULL "
                "ORDER BY bucket, output_tps",
                (str(window_minutes),),
            ).fetchall()
            result = []
            by_bucket = {}
            for bucket, tps in rows:
                by_bucket.setdefault(bucket, []).append(tps)
            for bucket, vals in by_bucket.items():
                if not vals:
                    continue
                n = len(vals)

                def percentile(p):
                    # Nearest-rank percentile: ceil(p*n/100)-1.
                    k = max(0, min(n - 1, (p * n + 99) // 100 - 1))
                    return vals[k]

                result.append({
                    "bucket": bucket,
                    "p50": round(percentile(50), 2),
                    "p95": round(percentile(95), 2),
                    "p99": round(percentile(99), 2),
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

    def get_perf_models(self, window_minutes: int = 60, max_samples: int = 100) -> list[dict]:
        """Per-model observed TTFT and weighted output speed.

        Samples only the *max_samples* most recent request_log rows per model
        (within the window), so high-traffic models don't dominate the
        averages; `ttft_samples`/`speed_samples` report how many of those rows
        actually carried a TTFT / speed observation.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "WITH ranked AS ("
                "  SELECT model, status_code, ttft_ms, generation_ms, completion_tokens, "
                "         ROW_NUMBER() OVER (PARTITION BY model "
                "                            ORDER BY requested_at DESC, id DESC) AS rn "
                "  FROM request_log "
                "  WHERE requested_at >= datetime('now', '-' || ? || ' minutes')"
                ") "
                "SELECT model, COUNT(*) AS request_count, "
                "AVG(CASE WHEN status_code BETWEEN 200 AND 299 THEN ttft_ms END) AS avg_ttft_ms, "
                "MAX(CASE WHEN status_code BETWEEN 200 AND 299 THEN ttft_ms END) AS max_ttft_ms, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND ttft_ms IS NOT NULL "
                "         THEN 1 ELSE 0 END) AS ttft_samples, "
                "CASE WHEN SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                              AND completion_tokens > 1 "
                "                   THEN generation_ms ELSE 0 END) > 0 "
                "THEN SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                   AND completion_tokens > 1 "
                "              THEN completion_tokens - 1 ELSE 0 END) * 1000.0 / "
                "     SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "                   AND completion_tokens > 1 "
                "              THEN generation_ms ELSE 0 END) END AS avg_output_tps, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 AND generation_ms > 0 "
                "         AND completion_tokens > 1 THEN 1 ELSE 0 END) AS speed_samples, "
                "SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS success_count "
                "FROM ranked "
                "WHERE rn <= ? "
                "GROUP BY model "
                "ORDER BY request_count DESC",
                (str(window_minutes), max_samples),
            ).fetchall()

            return [{
                "model": r[0],
                "requests": r[1],
                # NULL means no semantic TTFT was observed (for example a
                # non-streaming request), not a zero-millisecond response.
                "avg_ttft_ms": round(r[2], 1) if r[2] is not None else None,
                "max_ttft_ms": r[3] if r[3] is not None else None,
                "ttft_samples": r[4] or 0,
                "avg_output_tps": round(r[5], 2) if r[5] is not None else None,
                "speed_samples": r[6] or 0,
                "success_rate": round(r[7] / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_upstream_success_rate(self, window_minutes: int = 60) -> list[dict]:
        """Per real-upstream success rate for the last N minutes.

        'Real upstream' = non-aggregate accounts (is_aggregate via JOIN).
        Success = HTTP 2xx.  Attempt rows are authoritative when present;
        legacy request rows are used only for requests that reached an upstream.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "WITH observations(account_id, status_code, requested_at) AS ("
                "  SELECT account_id, status_code, requested_at FROM request_attempts "
                "  WHERE requested_at >= datetime('now', '-' || ? || ' minutes') "
                "  UNION ALL "
                "  SELECT r.account_id, r.status_code, r.requested_at FROM request_log r "
                "  WHERE COALESCE(r.attempt_count, 1) > 0 "
                "    AND r.requested_at >= datetime('now', '-' || ? || ' minutes') "
                "    AND NOT EXISTS (SELECT 1 FROM request_attempts t "
                "                    WHERE t.request_log_id = r.id)"
                ") "
                "SELECT COALESCE(a.name, 'unknown') AS account_name, "
                "COUNT(*) AS total, "
                "SUM(CASE WHEN o.status_code BETWEEN 200 AND 299 "
                "         THEN 1 ELSE 0 END) AS successes "
                "FROM observations o "
                "LEFT JOIN upstream_accounts a ON a.id = o.account_id "
                "WHERE COALESCE(a.is_aggregate, 0) = 0 "
                "  AND o.status_code != 499 "
                "GROUP BY o.account_id "
                "ORDER BY total DESC",
                (str(window_minutes), str(window_minutes)),
            ).fetchall()

            return [{
                "account_name": r[0],
                "total": r[1],
                "errors": r[1] - (r[2] or 0),
                "success_rate": round((r[2] or 0) / max(r[1], 1) * 100, 1),
            } for r in rows]
        finally:
            conn.close()

    def get_perf_realtime(self, window_seconds: int = 60) -> dict:
        """Real-time metrics: current RPM estimate and live concurrency.

        RPM is estimated from request_log.  Live concurrency comes from the
        proxy's process-local counter so request forwarding never writes an
        observability row before contacting the upstream.  An unreachable
        proxy is reported as unavailable rather than the misleading zero from
        the legacy table.
        """
        conn = self._connect()
        try:
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM request_log "
                "WHERE requested_at >= datetime('now', '-' || ? || ' seconds')",
                (str(window_seconds),)
            ).fetchone()[0]

            rpm = round(recent_count / max(window_seconds / 60.0, 0.1), 1)

            latest_concurrent = None
            proxy_port = os.environ.get("TOKEN_PROXY_PORT", "8800")
            health_url = os.environ.get(
                "TOKEN_PROXY_HEALTH_URL", f"http://127.0.0.1:{proxy_port}/health"
            )
            try:
                with urllib.request.urlopen(health_url, timeout=1.0) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if isinstance(health.get("concurrency"), int):
                    latest_concurrent = health["concurrency"]
            except (OSError, ValueError, json.JSONDecodeError):
                pass

            # The in_flight_requests table was dropped in migration 0017; live
            # concurrency comes from /health above.
            return {
                "rpm": rpm,
                "recent_requests": recent_count,
                "latest_concurrent": latest_concurrent,
                "in_flight": [],
            }
        finally:
            conn.close()
