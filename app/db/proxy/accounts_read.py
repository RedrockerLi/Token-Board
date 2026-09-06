"""ProxyDatabase methods for ProxyAccountReadMixin."""

from app.core.time import parse_runtime_timestamp, utc_now
from app.db.proxy.common import (
    ACCOUNT_TYPES, UTC, _billing_period_month, _cancellation_end,
    _parse_iso_date, _period_start, _subscription_date,
    billing_period, ConflictError, datetime, json, mask_key, sqlite3, uuid,
)
from app.domain.account_template import AccountTemplate, AccountTemplateAdapter


class ProxyAccountReadMixin:
    def get_accounts(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                    "SELECT 1 routed,rs.id,a.name,u.base_url,u.api_format,"
                    "COALESCE(u.endpoint_path,'') endpoint_path,"
                    "COALESCE(u.auth_scheme,'bearer') auth_header,"
                    "COALESCE((SELECT recurring_price FROM billing_rate_events "
                    "WHERE contract_id=bc.id ORDER BY effective_at DESC,id DESC LIMIT 1),0) recurring_price,"
                    "bc.charge_type,"
                    "COALESCE(bc.currency,'CNY') currency,"
                    "COALESCE(a.valid_from,'') valid_from,u.max_concurrency,a.created_at,"
                    "bc.ends_at,"
                    "(SELECT count(DISTINCT c.key_masked) FROM upstream_credentials c "
                    "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                    "WHERE c.upstream_id=u.id "
                    "AND (c.ends_at IS NULL OR c.ends_at>"
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "AND (bc.charge_type='recurring' OR s.secret_value IS NOT NULL) "
                    "AND c.enabled=1 AND (c.ends_at IS NULL OR c.ends_at>"
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now'))) key_count "
                    "FROM route_sets rs JOIN accounts a ON a.id=rs.account_id "
                    "JOIN upstreams u ON u.account_id=a.id AND u.enabled=1 "
                    "LEFT JOIN billing_contracts bc ON bc.account_id=a.id "
                    "AND (bc.ends_at IS NULL OR bc.ends_at>strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "WHERE rs.enabled=1 AND a.account_kind='proxy' "
                    ""
                ).fetchall()
            adapter = AccountTemplateAdapter()
            accounts = []
            for row in rows:
                row = dict(row)
                routed = bool(row.pop("routed"))
                recurring = row.get("charge_type") == "recurring"
                if routed:
                    template = adapter.routed(row, recurring=recurring)
                else:
                    # This branch is retained only for malformed historical
                    # rows; importer-only agent data is now separate.
                    continue
                acc = template.to_dict()
                route = self._v1_route_account(conn, acc["id"])
                local_keys: list[dict] = []
                cloud_keys: list[dict] = []
                if route and route["upstream_id"] is not None:
                    rows = conn.execute(
                        "SELECT c.runtime_id,s.secret_value,c.key_masked,c.valid_from,c.ends_at "
                        "FROM upstream_credentials c LEFT JOIN upstream_secrets s "
                        "ON s.credential_uuid=c.uuid WHERE c.upstream_id=? "
                        "AND (c.ends_at IS NULL OR c.ends_at>"
                        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                        "AND c.enabled=1 AND (c.ends_at IS NULL OR c.ends_at>"
                        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                        "ORDER BY c.position,c.runtime_id",
                        (route["upstream_id"],),
                    ).fetchall()
                    for key in rows:
                        if key["secret_value"] is not None:
                            local_keys.append({
                                "id": key["runtime_id"],
                                "masked": key["key_masked"],
                                "valid_from": key["valid_from"],
                                "ends_at": key["ends_at"],
                            })
                        else:
                            cloud_keys.append({
                                "masked": key["key_masked"],
                                "valid_from": key["valid_from"],
                            })
                    local_masked = {key["masked"] for key in local_keys}
                    cloud_keys = [key for key in cloud_keys
                                  if key["masked"] not in local_masked]
                acc["keys"] = local_keys
                acc["cloud_keys"] = cloud_keys
                accounts.append(acc)
            # Aggregate route sets are valid local-key targets (they expose a
            # model catalog and resolve to real upstreams per request), so
            # they belong in the accounts payload exactly like the pre-V1 API.
            for row in conn.execute(
                "SELECT id,name,created_at FROM route_sets "
                "WHERE account_id IS NULL AND enabled=1 ORDER BY id"):
                accounts.append(AccountTemplate(
                    id=int(row["id"]), name=row["name"],
                    is_aggregate=True, account_type="api",
                    created_at=row["created_at"],
                ).to_dict())
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
        row = conn.execute(
            "SELECT COALESCE((SELECT value FROM sync_settings WHERE key='billing.cancellation_mode'),'end_of_period') "
            "AS cancellation_mode"
        ).fetchone()
        # Legacy V0/V1 settings may use `period_end`, while the settings API
        # and UI use `end_of_period`. Normalize that legacy value here so every
        # caller sees the same public mode.
        return {
            "cancellation_mode": (
                "end_of_period" if row["cancellation_mode"] == "period_end"
                else row["cancellation_mode"]
            ),
        }

    @staticmethod
    def _refresh_upstream_keys_cloud(conn: sqlite3.Connection, account_id: int) -> None:
        """V1 credential metadata is already safe for cloud synchronization."""
        return

    def _set_upstream_keys(self, conn: sqlite3.Connection, account_id: int,
                           keep_ids: list[int], new_keys: list[str],
                           keep_valid_froms: dict[str, object] | None = None,
                           new_valid_froms: list[object] | None = None,
                           account_type: str = "api") -> list[str]:
        """Diff an account's local keys using physical deletion.

        Returns active plaintext values only so the legacy account column can
        retain its first active fallback key.  This function is called inside
        the same transaction that updates the cloud-safe metadata mirror.

        Removing a key is a live-configuration operation.  Its request rows
        retain their identity snapshot while the credential row is deleted.
        """
        keep_valid_froms = keep_valid_froms or {}
        new_valid_froms = new_valid_froms or []
        route = self._v1_route_account(conn, account_id)
        if route is None or route["upstream_id"] is None:
            raise ValueError("账户没有可写入密钥的上游")
        if account_type == "plan":
            ending = conn.execute(
                "SELECT ends_at FROM billing_contracts WHERE account_id=? "
                "AND charge_type='recurring' AND ends_at>? ORDER BY id DESC LIMIT 1",
                (account_id, utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")),
            ).fetchone()
            if ending is not None:
                raise ConflictError("账户已进入结束流程，不能新增 Key")
        upstream_id = int(route["upstream_id"])
        active = {
            row["runtime_id"]: row for row in conn.execute(
                "SELECT c.runtime_id,c.uuid,c.valid_from,c.created_at,s.secret_value "
                "FROM upstream_credentials c LEFT JOIN upstream_secrets s "
                "ON s.credential_uuid=c.uuid WHERE c.upstream_id=? "
                "AND (c.ends_at IS NULL OR c.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (upstream_id,))
        }
        retained = [key_id for key_id in keep_ids if key_id in active]
        for key_id, row in active.items():
            if key_id not in retained:
                conn.execute(
                    "UPDATE request_log SET credential_uuid=NULL,upstream_key_id=NULL "
                    "WHERE credential_uuid=?", (row["uuid"],))
                conn.execute(
                    "UPDATE request_attempts SET credential_uuid=NULL,upstream_key_id=NULL "
                    "WHERE credential_uuid=?", (row["uuid"],))
                conn.execute("DELETE FROM upstream_secrets WHERE credential_uuid=?",
                             (row["uuid"],))
                conn.execute(
                    "DELETE FROM upstream_credentials WHERE runtime_id=?", (key_id,),
                )
        active_values = []
        position = 0
        for key_id in retained:
            raw_valid = keep_valid_froms.get(str(key_id))
            valid = (_parse_iso_date(_subscription_date(raw_valid))
                     if raw_valid not in (None, "") else None)
            conn.execute(
                "UPDATE upstream_credentials SET position=?,valid_from=? WHERE runtime_id=?",
                (position, valid.isoformat() if valid else None, key_id),
            )
            if active[key_id]["secret_value"]:
                active_values.append(active[key_id]["secret_value"])
            position += 1
        seen = set(active_values)
        next_runtime = int(conn.execute(
            "SELECT COALESCE(max(runtime_id),0)+1 FROM upstream_credentials"
        ).fetchone()[0])
        for index, raw_key in enumerate(new_keys):
            key = raw_key.strip()
            if not key or key in seen:
                continue
            raw_valid = (new_valid_froms[index]
                         if index < len(new_valid_froms) else None)
            valid = (_parse_iso_date(_subscription_date(raw_valid))
                     if raw_valid not in (None, "") else None)
            credential_uuid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO upstream_credentials"
                "(uuid,runtime_id,upstream_id,position,key_masked,valid_from,enabled) "
                "VALUES(?,?,?,?,?,?,1)",
                (credential_uuid, next_runtime, upstream_id, position,
                 mask_key(key), valid.isoformat() if valid else None),
            )
            conn.execute(
                "INSERT INTO upstream_secrets(credential_uuid,secret_value) VALUES(?,?)",
                (credential_uuid, key),
            )
            active_values.append(key)
            seen.add(key)
            next_runtime += 1
            position += 1
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
            route = self._v1_route_account(conn, account_id)
            if route is None or route["upstream_id"] is None:
                return False
            cloud = conn.execute(
                "SELECT c.uuid,c.valid_from,s.secret_value FROM upstream_credentials c "
                "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                "WHERE c.upstream_id=? AND c.key_masked=? "
                "AND (c.ends_at IS NULL OR c.ends_at>strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (route["upstream_id"], masked),
            ).fetchone()
            if cloud is None:
                return False
            if mask_key(key_value) != masked:
                raise ValueError("密钥明文与云端掩码不匹配")
            conn.execute(
                "INSERT INTO upstream_secrets(credential_uuid,secret_value) VALUES(?,?) "
                "ON CONFLICT(credential_uuid) DO UPDATE SET secret_value=excluded.secret_value,"
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (cloud["uuid"], key_value),
            )
            conn.commit()
            return True
        finally:
            conn.close()
