"""ProxyDatabase methods for ProxyAccountReadMixin."""

from app.db.proxy.common import *  # noqa: F401,F403
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
                    "COALESCE(i.importer_kind,'') importer_kind,"
                    "COALESCE(a.valid_from,'') valid_from,u.max_concurrency,a.created_at,a.deleted_at,"
                    "(SELECT count(*) FROM upstream_credentials c "
                    "LEFT JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                    "WHERE c.upstream_id=u.id "
                    "AND (c.deleted_at IS NULL OR c.deleted_at>"
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "AND s.secret_value IS NOT NULL "
                    "AND (c.disabled_at IS NULL OR c.disabled_at>"
                    "strftime('%Y-%m-%dT%H:%M:%fZ','now'))) key_count "
                    "FROM route_sets rs JOIN accounts a ON a.id=rs.account_id "
                    "JOIN upstreams u ON u.account_id=a.id AND u.enabled=1 "
                    "LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL "
                    "LEFT JOIN account_importers i ON i.account_id=a.id AND i.enabled=1 "
                    "WHERE rs.enabled=1 AND a.lifecycle_state='active' "
                    "UNION ALL "
                    "SELECT 0 routed,a.id,a.name,'','openai','','auto',"
                    "COALESCE((SELECT recurring_price FROM billing_rate_events "
                    "WHERE contract_id=bc.id ORDER BY effective_at DESC,id DESC LIMIT 1),0),"
                    "bc.charge_type,"
                    "COALESCE(bc.currency,'CNY'),i.importer_kind,COALESCE(a.valid_from,''),"
                    "0,a.created_at,a.deleted_at,0 "
                    "FROM account_importers i JOIN accounts a ON a.id=i.account_id "
                    "LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL "
                    "WHERE i.enabled=1 AND a.lifecycle_state='active'"
                ).fetchall()
            adapter = AccountTemplateAdapter()
            accounts = []
            for row in rows:
                row = dict(row)
                routed = bool(row.pop("routed"))
                importer_kind = row.get("importer_kind") or ""
                recurring = row.get("charge_type") == "recurring"
                if routed:
                    template = adapter.routed(
                        row, recurring=recurring, importer_kind=importer_kind)
                else:
                    template = adapter.agent_only(
                        row, importer_kind,
                        float(row.get("recurring_price") or 0),
                        row.get("currency") or "CNY")
                acc = template.to_dict()
                route = self._v1_route_account(conn, acc["id"])
                local_keys: list[dict] = []
                cloud_keys: list[dict] = []
                if route and route["upstream_id"] is not None:
                    rows = conn.execute(
                        "SELECT c.runtime_id,s.secret_value,c.key_masked,c.valid_from,c.deleted_at "
                        "FROM upstream_credentials c LEFT JOIN upstream_secrets s "
                        "ON s.credential_uuid=c.uuid WHERE c.upstream_id=? "
                        "AND (c.deleted_at IS NULL OR c.deleted_at>"
                        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                        "AND (c.disabled_at IS NULL OR c.disabled_at>"
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
                                "deleted_at": key["deleted_at"],
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
        return conn.execute(
            "SELECT COALESCE((SELECT value FROM sync_settings WHERE key='billing.price_change_effective'),'current_period') "
            "AS price_change_effective,COALESCE((SELECT value FROM sync_settings WHERE key='billing.cancellation_mode'),'period_end') "
            "AS cancellation_mode"
        ).fetchone()

    @staticmethod
    def _refresh_upstream_keys_cloud(conn: sqlite3.Connection, account_id: int) -> None:
        """V1 credential metadata is already safe for cloud synchronization."""
        return

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
        route = ProxyDatabase._v1_route_account(conn, account_id)
        if route is None or route["upstream_id"] is None:
            raise ValueError("账户没有可写入密钥的上游")
        upstream_id = int(route["upstream_id"])
        active = {
            row["runtime_id"]: row for row in conn.execute(
                "SELECT c.runtime_id,c.uuid,c.valid_from,c.created_at,s.secret_value "
                "FROM upstream_credentials c LEFT JOIN upstream_secrets s "
                "ON s.credential_uuid=c.uuid WHERE c.upstream_id=? "
                "AND c.deleted_at IS NULL", (upstream_id,))
        }
        retained = [key_id for key_id in keep_ids if key_id in active]
        config = ProxyDatabase._billing_config_conn(conn)
        now = _utc_now()
        for key_id, row in active.items():
            if key_id not in retained:
                anchor = (_parse_iso_date(row["valid_from"])
                          or _parse_utc_timestamp(row["created_at"]).date())
                end = _cancellation_end(config, now, anchor.day, account_type)
                conn.execute(
                    "UPDATE upstream_credentials SET deleted_at=? WHERE runtime_id=?",
                    (end.strftime("%Y-%m-%dT%H:%M:%SZ"), key_id),
                )
        active_values = []
        position = 0
        for key_id in retained:
            valid = _parse_iso_date(keep_valid_froms.get(str(key_id)))
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
            valid = _parse_iso_date(
                new_valid_froms[index] if index < len(new_valid_froms) else None)
            credential_uuid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO upstream_credentials"
                "(uuid,runtime_id,upstream_id,position,key_masked,valid_from) "
                "VALUES(?,?,?,?,?,?)",
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
                "WHERE c.upstream_id=? AND c.key_masked=? AND c.deleted_at IS NULL",
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
