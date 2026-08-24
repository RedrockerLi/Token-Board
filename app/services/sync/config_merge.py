"""Functional WebDAV synchronization module."""

from app.services.sync.common import *  # noqa: F401,F403
from app.services.sync.settings import SyncConfig

import logging

log = logging.getLogger(__name__)


def _schema_major(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT major FROM schema_version WHERE id=1").fetchone()
    return int(row[0]) if row else 0

def _merge_config_tables(remote_path: str, local_path: str) -> None:
    """Merge a downloaded, already-upgraded V1 artifact into local V1.

    V0 artifacts are upgraded by :mod:`app.db.schema_upgrade` before this
    function is called.  Keeping that boundary explicit prevents the running
    sync path from accidentally writing legacy tables or secrets.
    """
    probe_remote = sqlite3.connect(remote_path)
    probe_local = sqlite3.connect(local_path)
    try:
        remote_major = _schema_major(probe_remote)
        local_major = _schema_major(probe_local)
    finally:
        probe_remote.close()
        probe_local.close()
    if remote_major != 1 or local_major != 1:
        raise RuntimeError(
            "配置合并只接受 V1 shadow；请先通过 schema upgrade coordinator "
            f"转换 remote=V{remote_major}, local=V{local_major}")
    if remote_major != local_major:
        raise RuntimeError(
            f"配置同步拒绝跨 Major 合并: remote=V{remote_major}, local=V{local_major}")
    _merge_v1_config(remote_path, local_path)


def _merge_v1_config(remote_path: str, local_path: str) -> None:
    """Merge normalized V1 configuration without importing local secrets.

    Stable UUIDs are authoritative. Missing proxy rows become lifecycle
    tombstones rather than being deleted, preserving request/attempt foreign
    keys and historical pricing; agent software rows are physically deleted by
    the local management API and their account tombstone is only a sync
    fallback. ``runtime_id`` and importer cursors remain local.
    """
    remote = sqlite3.connect(remote_path)
    remote.row_factory = sqlite3.Row
    local = sqlite3.connect(local_path, timeout=10)
    local.row_factory = sqlite3.Row
    try:
        local.execute("PRAGMA busy_timeout=5000")
        local.execute("PRAGMA foreign_keys=ON")
        local.execute("BEGIN IMMEDIATE")

        def table_info(conn, table):
            return conn.execute(f"PRAGMA table_info({table})").fetchall()

        def merge_table(table: str, excluded: set[str] | None = None) -> None:
            excluded = excluded or set()
            if not _table_exists(remote, table) or not _table_exists(local, table):
                return
            r_info = table_info(remote, table)
            local_columns = {row[1] for row in table_info(local, table)}
            columns = [row[1] for row in r_info
                       if row[1] in local_columns and row[1] not in excluded]
            primary = [row[1] for row in sorted(r_info, key=lambda row: row[5]) if row[5]]
            if not columns or not primary:
                return
            updates = [column for column in columns if column not in primary]
            placeholders = ",".join("?" for _ in columns)
            conflict = ",".join(primary)
            suffix = (" DO UPDATE SET " + ",".join(
                f"{column}=excluded.{column}" for column in updates)) if updates else " DO NOTHING"
            statement = (f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) "
                         f"ON CONFLICT({conflict}){suffix}")
            for row in remote.execute(f"SELECT {','.join(columns)} FROM {table}"):
                local.execute(statement, tuple(row))

        # Parent-first upsert. Historical rows not present remotely remain as
        # inactive/local history instead of violating live request FKs.
        for table in ("accounts", "upstreams", "route_sets", "route_rules",
                      "client_keys"):
            merge_table(table)

        remote_credential_ids: set[str] = set()
        if _table_exists(remote, "upstream_credentials"):
            next_runtime = int(local.execute(
                "SELECT COALESCE(max(runtime_id),0)+1 FROM upstream_credentials"
            ).fetchone()[0])
            for row in remote.execute("SELECT * FROM upstream_credentials ORDER BY position,uuid"):
                remote_credential_ids.add(row["uuid"])
                existing = local.execute(
                    "SELECT runtime_id FROM upstream_credentials WHERE uuid=?",
                    (row["uuid"],),
                ).fetchone()
                runtime_id = existing[0] if existing else next_runtime
                if existing is None:
                    next_runtime += 1
                local.execute(
                    "INSERT INTO upstream_credentials"
                    "(uuid,runtime_id,upstream_id,position,key_masked,valid_from,created_at,disabled_at,deleted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET "
                    "upstream_id=excluded.upstream_id,position=excluded.position,"
                    "key_masked=excluded.key_masked,valid_from=excluded.valid_from,"
                    "disabled_at=excluded.disabled_at,deleted_at=excluded.deleted_at",
                    (row["uuid"], runtime_id, row["upstream_id"], row["position"],
                     row["key_masked"], row["valid_from"], row["created_at"],
                     row["disabled_at"], row["deleted_at"]),
                )

        merge_table("account_importers", {"cursor_json"})
        for table in ("billing_contracts", "billing_rate_events", "pricing_rules",
                      "pricing_rates", "pricing_slots", "upstream_model_catalog",
                      "proxy_timeout_config", "agent_subscriptions", "agent_software",
                      "agent_subscription_instances",
                      "agent_subscription_rate_events",
                      "agent_subscription_bindings"):
            merge_table(table)

        # A migrated agent used to be represented by an upstream billing
        # contract.  Remove that legacy contract after the cloud merge as well
        # so an older cloud artifact cannot reintroduce the duplicate fee.
        legacy_contract_ids = [row[0] for row in local.execute(
            "SELECT bc.id FROM billing_contracts bc "
            "JOIN account_importers i ON i.account_id=bc.account_id "
            "WHERE i.enabled=0 AND i.importer_kind IS NOT NULL"
        ).fetchall()]
        if legacy_contract_ids:
            placeholders = ",".join("?" for _ in legacy_contract_ids)
            for table in ("billing_period_charges", "billing_rate_events"):
                local.execute(
                    f"DELETE FROM {table} WHERE contract_id IN ({placeholders})",
                    legacy_contract_ids,
                )
            local.execute(
                f"DELETE FROM billing_contracts WHERE id IN ({placeholders})",
                legacy_contract_ids,
            )

        # WebDAV credentials are needed locally to reach the cloud and must
        # never be copied between machines.  The non-secret connection
        # settings may still be synchronized.
        if _table_exists(remote, "sync_settings") and _table_exists(local, "sync_settings"):
            for row in remote.execute(
                    "SELECT key,value FROM sync_settings "
                    "WHERE key NOT IN ('password','agent_migration_v1_6')"):
                local.execute(
                    "INSERT INTO sync_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    tuple(row),
                )

        remote_ids = {
            table: {tuple(row) for row in remote.execute(
                "SELECT " + ",".join(
                    info[1] for info in sorted(table_info(remote, table), key=lambda value: value[5])
                    if info[5]) + f" FROM {table}")}
            for table in ("accounts", "upstreams", "route_sets", "route_rules",
                          "client_keys", "account_importers", "pricing_rules",
                          "agent_subscriptions", "agent_software",
                          "agent_subscription_instances", "agent_subscription_bindings")
            if _table_exists(remote, table)
        }
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tombstones = {
            "accounts": (
                "lifecycle_state=CASE WHEN account_kind='agent' THEN 'deleted' "
                "ELSE 'disabled' END,"
                "disabled_at=CASE WHEN account_kind='agent' THEN NULL ELSE ? END,"
                "deleted_at=CASE WHEN account_kind='agent' THEN ? ELSE deleted_at END,"
                "updated_at=?",
                (now, now, now),
            ),
            "upstreams": ("enabled=0", ()), "route_sets": ("enabled=0", ()),
            "route_rules": ("enabled=0", ()), "client_keys": ("enabled=0", ()),
            "account_importers": ("enabled=0", ()), "pricing_rules": ("enabled=0", ()),
            "agent_subscriptions": ("lifecycle_state='deleted',valid_until=?", (now,)),
            "agent_software": ("enabled=1", ()),
            "agent_subscription_instances": (
                "lifecycle_state='deleted',valid_until=?,updated_at=?", (now, now)),
            "agent_subscription_bindings": (
                "lifecycle_state='deleted',valid_until=?,updated_at=?", (now, now)),
        }
        for table, identities in remote_ids.items():
            info = table_info(local, table)
            primary = [row[1] for row in sorted(info, key=lambda value: value[5]) if row[5]]
            for row in local.execute(f"SELECT {','.join(primary)} FROM {table}").fetchall():
                identity = tuple(row)
                if identity not in identities:
                    clause, params = tombstones[table]
                    where = " AND ".join(f"{column}=?" for column in primary)
                    local.execute(f"UPDATE {table} SET {clause} WHERE {where}",
                                  (*params, *identity))
        for row in local.execute("SELECT uuid FROM upstream_credentials").fetchall():
            if row["uuid"] not in remote_credential_ids:
                local.execute(
                    "UPDATE upstream_credentials SET disabled_at=COALESCE(disabled_at,?) "
                    "WHERE uuid=?",
                    (now, row["uuid"]))
        local.execute("UPDATE config_state SET generation=generation+1 WHERE id=1")
        violation = local.execute("PRAGMA foreign_key_check").fetchone()
        if violation:
            raise sqlite3.IntegrityError(f"V1 config merge FK violation: {tuple(violation)}")
        local.commit()
    except Exception:
        local.rollback()
        raise
    finally:
        remote.close()
        local.close()


def _sanitize_upload_columns(dst: sqlite3.Connection) -> None:
    """Remove machine-local runtime and sensitive credential values."""
    if _table_exists(dst, "account_importers"):
        dst.execute("UPDATE account_importers SET cursor_json='{}'")
    if _table_exists(dst, "upstream_secrets"):
        dst.execute("DELETE FROM upstream_secrets")
    if (_table_exists(dst, "billing_contracts") and
            _table_exists(dst, "account_importers")):
        legacy_contract_ids = [row[0] for row in dst.execute(
            "SELECT bc.id FROM billing_contracts bc "
            "JOIN account_importers i ON i.account_id=bc.account_id "
            "WHERE i.enabled=0 AND i.importer_kind IS NOT NULL"
        ).fetchall()]
        if legacy_contract_ids:
            placeholders = ",".join("?" for _ in legacy_contract_ids)
            for table in ("billing_period_charges", "billing_rate_events"):
                if _table_exists(dst, table):
                    dst.execute(
                        f"DELETE FROM {table} WHERE contract_id IN ({placeholders})",
                        legacy_contract_ids,
                    )
            dst.execute(
                f"DELETE FROM billing_contracts WHERE id IN ({placeholders})",
                legacy_contract_ids,
            )
    # This marker is created by the local one-time migration, not configured
    # by the user. Do not make a machine's migration state cloud data.
    if _table_exists(dst, "sync_settings"):
        dst.execute(
            "DELETE FROM sync_settings WHERE key IN ('password','agent_migration_v1_6')"
        )
