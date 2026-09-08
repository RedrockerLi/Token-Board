"""Cloud-authoritative configuration merge for synchronized V2 tables."""

import sqlite3

from app.core import sqlite_runtime
from app.core.time import utc_now
from app.services.sync.state import table_exists

import logging

log = logging.getLogger(__name__)


def _schema_major(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT major FROM schema_version WHERE id=1").fetchone()
    return int(row[0]) if row else 0


def merge_config_tables(remote_path: str, local_path: str) -> None:
    """Merge a downloaded, already-upgraded V2 artifact into local V2.

    V0 artifacts are upgraded by :mod:`app.db.schema_upgrade` before this
    function is called.  Keeping that boundary explicit prevents the running
    sync path from accidentally writing legacy tables or secrets.
    """
    probe_remote = sqlite_runtime.connect(remote_path, "shadow_copy")
    probe_local = sqlite_runtime.connect(local_path, "proxy_runtime")
    try:
        remote_major = _schema_major(probe_remote)
        local_major = _schema_major(probe_local)
    finally:
        probe_remote.close()
        probe_local.close()
    if remote_major != 2 or local_major != 2:
        raise RuntimeError(
            "配置合并只接受 V2 shadow；请先通过 schema upgrade coordinator "
            f"转换 remote=V{remote_major}, local=V{local_major}")
    if remote_major != local_major:
        raise RuntimeError(
            f"配置同步拒绝跨 Major 合并: remote=V{remote_major}, local=V{local_major}")
    _merge_v2_config(remote_path, local_path)


def _merge_v2_config(remote_path: str, local_path: str) -> None:
    """Merge normalized V2 configuration without importing local secrets.

    Stable UUIDs are authoritative. Missing proxy and Agent live rows are
    hard-deleted after the current Agent charge is materialized. Proxy
    accounts, Agent subscriptions and Agent software retain their separate
    historical identities and ledgers while live configuration is removed.
    ``runtime_id`` and importer cursors remain local.
    """
    remote = sqlite_runtime.connect(remote_path, "shadow_copy")
    local = sqlite_runtime.connect(local_path, "proxy_runtime")
    try:
        local.execute("BEGIN IMMEDIATE")

        def table_info(conn, table):
            return conn.execute(f"PRAGMA table_info({table})").fetchall()

        def merge_table(table: str, excluded: set[str] | None = None) -> None:
            excluded = excluded or set()
            if not table_exists(remote, table) or not table_exists(local, table):
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

        def merge_natural_table(table: str,
                                natural_columns: tuple[str, ...]) -> None:
            """Merge Agent interval configuration by its date identity.

            ``id`` is a local surrogate for these two tables.  A price/binding
            row created on another machine can therefore have a different id
            while still describing the same date interval.  Cloud values win
            for that natural key, but the local surrogate is retained so no
            historical reference is rewritten.
            """
            if not table_exists(remote, table) or not table_exists(local, table):
                return
            remote_info = table_info(remote, table)
            local_columns = {row[1] for row in table_info(local, table)}
            columns = [row[1] for row in remote_info if row[1] in local_columns]
            updates = [column for column in columns
                       if column not in natural_columns and column != "id"]
            insert_columns = [column for column in columns if column != "id"]
            insert_sql = (
                f"INSERT INTO {table}({','.join(insert_columns)}) "
                f"VALUES({','.join('?' for _ in insert_columns)})"
            )
            for row in remote.execute(f"SELECT {','.join(columns)} FROM {table}"):
                natural = tuple(row[column] for column in natural_columns)
                existing = local.execute(
                    f"SELECT id FROM {table} WHERE " +
                    " AND ".join(f"{column}=?" for column in natural_columns) +
                    " ORDER BY id LIMIT 1", natural,
                ).fetchone()
                if existing is None:
                    local.execute(insert_sql,
                                  tuple(row[column] for column in insert_columns))
                    continue
                if updates:
                    local.execute(
                        f"UPDATE {table} SET " +
                        ",".join(f"{column}=?" for column in updates) +
                        " WHERE id=?",
                        tuple(row[column] for column in updates) + (existing[0],),
                    )

        # Parent-first upsert. Historical rows not present remotely remain as
        # inactive/local history instead of violating live request FKs.
        for table in ("account_identities", "agent_subscription_identities",
                      "agent_subscription_instance_identities", "accounts",
                      "upstreams", "route_sets", "route_rules", "client_keys"):
            merge_table(table)

        remote_credential_ids: set[str] = set()
        if table_exists(remote, "upstream_credentials"):
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
                    "(uuid,runtime_id,upstream_id,position,key_masked,valid_from,created_at,enabled,ends_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET "
                    "upstream_id=excluded.upstream_id,position=excluded.position,"
                    "key_masked=excluded.key_masked,valid_from=excluded.valid_from,"
                    "enabled=excluded.enabled,ends_at=excluded.ends_at",
                    (row["uuid"], runtime_id, row["upstream_id"], row["position"],
                     row["key_masked"], row["valid_from"], row["created_at"],
                     row["enabled"], row["ends_at"]),
                )

        merge_table("account_importers", {"cursor_json"})
        for table in ("billing_contracts", "billing_rate_events", "pricing_rules",
                      "upstream_model_catalog",
                      "proxy_timeout_config", "agent_subscriptions", "agent_software",
                      "agent_subscription_instances"):
            merge_table(table)
        merge_natural_table(
            "agent_subscription_rate_events", ("instance_id", "effective_on"))
        merge_natural_table(
            "agent_subscription_bindings",
            ("subscription_id", "software_id", "valid_from"))
        for table in ("pricing_slots", "pricing_length_tiers"):
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
        if table_exists(remote, "sync_settings") and table_exists(local, "sync_settings"):
            for row in remote.execute(
                    "SELECT key,value FROM sync_settings "
                    "WHERE key NOT IN ('password','agent_migration_v1_6')"):
                local.execute(
                    "INSERT INTO sync_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    tuple(row),
                )

        natural_keys = {
            "agent_subscription_bindings":
                ("subscription_id", "software_id", "valid_from"),
            "agent_subscription_rate_events": ("instance_id", "effective_on"),
        }
        now = utc_now()
        # A remote artifact may remove an Agent row while its current period
        # charge still needs to be retained. Materialize before physically
        # deleting the live row; export recovery is keyed by the immutable
        # allocation source key and does not depend on that row remaining.
        from app.db.proxy.billing import materialize_agent_subscription_charges_conn
        materialize_agent_subscription_charges_conn(
            local, now, current_only=True)
        remote_ids = {
            table: {tuple(row) for row in remote.execute(
                "SELECT " + ",".join(natural_keys[table]) + f" FROM {table}")}
            if table in natural_keys else {tuple(row) for row in remote.execute(
                "SELECT " + ",".join(
                    info[1] for info in sorted(table_info(remote, table), key=lambda value: value[5])
                    if info[5]) + f" FROM {table}")}
            for table in ("accounts", "upstreams", "route_sets", "route_rules",
                          "client_keys", "account_importers", "upstream_model_catalog",
                          "agent_subscriptions", "agent_software",
                          "agent_subscription_instances", "agent_subscription_bindings",
                          "agent_subscription_rate_events")
            if table_exists(remote, table)
        }
        # These are live configuration rows with no historical ownership.
        # A cloud snapshot that omits them means the operator removed them;
        # purge them locally in dependency order instead of accumulating a
        # second tombstone copy.  Request and financial history is detached
        # from the live graph before the physical delete.
        hard_delete_order = (
            "route_rules", "client_keys", "agent_subscription_bindings",
            "upstream_model_catalog", "upstreams", "route_sets",
            "agent_subscription_rate_events", "agent_subscription_instances",
            "agent_subscriptions", "agent_software",
        )
        hard_delete_tables = frozenset(hard_delete_order)
        for table in hard_delete_order:
            if not table_exists(remote, table) or not table_exists(local, table):
                continue
            info = table_info(local, table)
            primary = [row[1] for row in sorted(info, key=lambda value: value[5]) if row[5]]
            if not primary:
                continue
            identities = remote_ids.get(table, set())
            selected_columns = primary + [
                column for column in natural_keys.get(table, ())
                if column not in primary
            ]
            for row in local.execute(
                    f"SELECT {','.join(selected_columns)} FROM {table}").fetchall():
                identity = tuple(row[column] for column in primary)
                comparison = (tuple(row[column] for column in natural_keys[table])
                              if table in natural_keys else identity)
                if comparison in identities:
                    continue
                where = " AND ".join(f"{column}=?" for column in primary)
                if table == "client_keys":
                    from app.db.proxy.deletion import purge_client_keys
                    purge_client_keys(local, [identity[0]])
                elif table == "agent_subscription_instances":
                    from app.db.proxy.deletion import purge_agent_subscription_instance
                    purge_agent_subscription_instance(local, int(identity[0]))
                elif table == "agent_subscription_bindings":
                    local.execute("DELETE FROM agent_subscription_bindings WHERE id=?",
                                  identity)
                elif table == "agent_subscriptions":
                    from app.db.proxy.deletion import purge_agent_subscription
                    purge_agent_subscription(local, int(identity[0]))
                elif table == "agent_software":
                    local.execute("DELETE FROM agent_software_runtime WHERE software_id=?", identity)
                    local.execute(f"DELETE FROM {table} WHERE {where}", identity)
                elif table == "upstream_model_catalog":
                    local.execute(f"DELETE FROM {table} WHERE {where}", identity)
                elif table == "upstreams":
                    upstream_id = identity[0]
                    local.execute(
                        "UPDATE request_attempts SET upstream_id=NULL,"
                        "upstream_key_id=NULL,credential_uuid=NULL WHERE upstream_id=?",
                        (upstream_id,),
                    )
                    for credential in local.execute(
                            "SELECT uuid FROM upstream_credentials WHERE upstream_id=?",
                            (upstream_id,)).fetchall():
                        from app.db.proxy.deletion import purge_credential
                        purge_credential(local, credential[0])
                    local.execute("DELETE FROM upstream_model_catalog WHERE upstream_id=?",
                                  (upstream_id,))
                    local.execute("DELETE FROM upstreams WHERE id=?", (upstream_id,))
                elif table == "route_sets":
                    from app.db.proxy.deletion import purge_route_sets
                    purge_route_sets(local, [identity[0]])
                else:
                    local.execute(f"DELETE FROM {table} WHERE {where}", identity)
        for table, identities in remote_ids.items():
            if table in hard_delete_tables:
                continue
            info = table_info(local, table)
            primary = [row[1] for row in sorted(info, key=lambda value: value[5]) if row[5]]
            for row in local.execute(f"SELECT {','.join(primary)} FROM {table}").fetchall():
                identity = tuple(row)
                if identity not in identities:
                    where = " AND ".join(f"{column}=?" for column in primary)
                    if table == "accounts":
                        kind = local.execute(
                            "SELECT account_kind FROM accounts WHERE id=?", identity).fetchone()
                        if kind is not None and kind[0] == "proxy":
                            from app.db.proxy.deletion import purge_proxy_account
                            purge_proxy_account(local, int(identity[0]))
                        elif kind is not None:
                            local.execute("UPDATE request_log SET account_id=NULL,"
                                          "account_identity_id=COALESCE(account_identity_id,?) "
                                          "WHERE account_id=?", (identity[0], identity[0]))
                            local.execute("UPDATE request_attempts SET account_id=NULL "
                                          "WHERE account_id=?", (identity[0],))
                            local.execute(
                                "DELETE FROM agent_subscription_bindings WHERE software_id=?",
                                identity,
                            )
                            local.execute("DELETE FROM agent_software_runtime WHERE software_id=?", identity)
                            local.execute("DELETE FROM agent_software WHERE id=?", identity)
                            local.execute("DELETE FROM accounts WHERE id=?", identity)
                    elif table == "upstream_credentials":
                        from app.db.proxy.deletion import purge_credential
                        purge_credential(local, identity[0])
                    else:
                        local.execute(f"DELETE FROM {table} WHERE {where}", identity)
        for row in local.execute("SELECT uuid FROM upstream_credentials").fetchall():
            if row["uuid"] not in remote_credential_ids:
                from app.db.proxy.deletion import purge_credential
                purge_credential(local, row["uuid"])

        # Model pricing has no lifecycle tombstone in the flattened schema.
        # A cloud-authoritative artifact that omits a rule therefore removes
        # it physically; child slots and tiers cascade from the rule.
        if table_exists(remote, "pricing_rules") and table_exists(local, "pricing_rules"):
            remote_rule_ids = {
                int(row[0]) for row in remote.execute("SELECT id FROM pricing_rules")
            }
            local_rule_ids = [
                int(row[0]) for row in local.execute("SELECT id FROM pricing_rules")
            ]
            for rule_id in local_rule_ids:
                if rule_id not in remote_rule_ids:
                    local.execute("DELETE FROM pricing_rules WHERE id=?", (rule_id,))

        local.execute("UPDATE config_state SET generation=generation+1 WHERE id=1")
        violation = local.execute("PRAGMA foreign_key_check").fetchone()
        if violation:
            raise sqlite3.IntegrityError(f"V2 config merge FK violation: {tuple(violation)}")
        local.commit()
    except Exception:
        local.rollback()
        raise
    finally:
        remote.close()
        local.close()


def sanitize_upload_columns(dst: sqlite3.Connection) -> None:
    """Remove machine-local runtime and sensitive credential values."""
    if table_exists(dst, "account_importers"):
        dst.execute("UPDATE account_importers SET cursor_json='{}'")
    if table_exists(dst, "upstream_secrets"):
        dst.execute("DELETE FROM upstream_secrets")
    if (table_exists(dst, "billing_contracts") and
            table_exists(dst, "account_importers")):
        legacy_contract_ids = [row[0] for row in dst.execute(
            "SELECT bc.id FROM billing_contracts bc "
            "JOIN account_importers i ON i.account_id=bc.account_id "
            "WHERE i.enabled=0 AND i.importer_kind IS NOT NULL"
        ).fetchall()]
        if legacy_contract_ids:
            placeholders = ",".join("?" for _ in legacy_contract_ids)
            for table in ("billing_period_charges", "billing_rate_events"):
                if table_exists(dst, table):
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
    if table_exists(dst, "sync_settings"):
        dst.execute(
            "DELETE FROM sync_settings WHERE key IN ('password','agent_migration_v1_6')"
        )
