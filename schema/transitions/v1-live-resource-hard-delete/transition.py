"""One-time cleanup of terminal soft-deleted proxy resources.

This transition is intentionally separate from the recurring lifecycle code:
it runs once through the shadow upgrade barrier, removes only proxy accounts
already marked ``lifecycle_state='deleted'``, and keeps the immutable identity,
request history and V1.17 frozen financial facts.
"""

from __future__ import annotations

import sqlite3

from app.db.schema_upgrade.transition_api import TransitionContext


TRANSITION_ID = "v1-live-resource-hard-delete"


def _soft_deleted_proxy_ids(conn: sqlite3.Connection) -> list[int]:
    return [int(row[0]) for row in conn.execute(
        "SELECT id FROM accounts WHERE account_kind='proxy' "
        "AND lifecycle_state='deleted' ORDER BY id"
    ).fetchall()]


def _purge_route_sets(conn: sqlite3.Connection, account_id: int) -> None:
    route_ids = [int(row[0]) for row in conn.execute(
        "SELECT id FROM route_sets WHERE account_id=?", (account_id,)
    ).fetchall()]
    if route_ids:
        placeholders = ",".join("?" for _ in route_ids)
        conn.execute(
            f"UPDATE request_log SET route_set_id=NULL "
            f"WHERE route_set_id IN ({placeholders})", route_ids)
        key_ids = [int(row[0]) for row in conn.execute(
            f"SELECT id FROM client_keys WHERE route_set_id IN ({placeholders})",
            route_ids,
        ).fetchall()]
        if key_ids:
            key_placeholders = ",".join("?" for _ in key_ids)
            conn.execute(
                f"UPDATE request_log SET client_key_id=NULL "
                f"WHERE client_key_id IN ({key_placeholders})", key_ids)
            conn.execute(
                f"DELETE FROM client_keys WHERE id IN ({key_placeholders})",
                key_ids)
        conn.execute(
            f"DELETE FROM route_rules WHERE route_set_id IN ({placeholders})",
            route_ids)
        conn.execute(
            f"DELETE FROM route_sets WHERE id IN ({placeholders})", route_ids)


def _purge_proxy_account(conn: sqlite3.Connection, account_id: int) -> None:
    """Delete one terminal proxy graph without touching immutable history."""
    upstream_ids = [int(row[0]) for row in conn.execute(
        "SELECT id FROM upstreams WHERE account_id=?", (account_id,)
    ).fetchall()]
    _purge_route_sets(conn, account_id)
    if upstream_ids:
        placeholders = ",".join("?" for _ in upstream_ids)
        # Aggregate route rules may still target this account's upstreams.
        conn.execute(
            f"DELETE FROM route_rules WHERE upstream_id IN ({placeholders})",
            upstream_ids)

    # request_log is historical, but its live foreign-key columns must be
    # detached before credentials/upstreams/accounts are removed.
    conn.execute(
        "UPDATE request_log SET account_identity_id="
        "COALESCE(account_identity_id,account_id),account_id=NULL,"
        "credential_uuid=NULL WHERE account_id=?", (account_id,))
    conn.execute(
        "UPDATE request_attempts SET upstream_id=NULL,credential_uuid=NULL "
        "WHERE account_id=? OR upstream_id IN ("
        "SELECT id FROM upstreams WHERE account_id=?)",
        (account_id, account_id),
    )

    conn.execute(
        "DELETE FROM upstream_secrets WHERE credential_uuid IN ("
        "SELECT c.uuid FROM upstream_credentials c JOIN upstreams u "
        "ON u.id=c.upstream_id WHERE u.account_id=?)", (account_id,))
    conn.execute(
        "DELETE FROM upstream_credentials WHERE upstream_id IN ("
        "SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
    conn.execute(
        "DELETE FROM upstream_model_catalog WHERE upstream_id IN ("
        "SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
    conn.execute("DELETE FROM account_importers WHERE account_id=?", (account_id,))
    conn.execute(
        "DELETE FROM billing_rate_events WHERE contract_id IN ("
        "SELECT id FROM billing_contracts WHERE account_id=?)", (account_id,))
    conn.execute("DELETE FROM billing_contracts WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM upstreams WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))


def apply(context: TransitionContext) -> None:
    if context.scope != "local-pair":
        raise RuntimeError(
            f"unsupported {TRANSITION_ID} transition scope: {context.scope}")
    conn = sqlite3.connect(context.shadow("token-board"))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for account_id in _soft_deleted_proxy_ids(conn):
            _purge_proxy_account(conn, account_id)
        conn.commit()
    finally:
        conn.close()


def verify(context: TransitionContext) -> dict:
    source = sqlite3.connect(context.source("token-board"))
    shadow = sqlite3.connect(context.shadow("token-board"))
    try:
        source_ids = _soft_deleted_proxy_ids(source)
        for account_id in source_ids:
            if shadow.execute(
                    "SELECT 1 FROM accounts WHERE id=?", (account_id,)
            ).fetchone() is not None:
                raise RuntimeError(
                    f"soft-deleted proxy account remains: {account_id}")
            if shadow.execute(
                    "SELECT 1 FROM account_identities WHERE id=?", (account_id,)
            ).fetchone() is None:
                raise RuntimeError(
                    f"historical account identity was lost: {account_id}")
            source_charge_count = source.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE account_identity_id=?", (account_id,)
            ).fetchone()[0]
            shadow_charge_count = shadow.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE account_identity_id=?", (account_id,)
            ).fetchone()[0]
            if source_charge_count != shadow_charge_count:
                raise RuntimeError(
                    f"frozen charge count changed for account {account_id}: "
                    f"{source_charge_count} -> {shadow_charge_count}")
        violation = shadow.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise RuntimeError(
                f"foreign_key_check failed after {TRANSITION_ID}: {tuple(violation)}")
        return {
            "deleted_proxy_accounts": len(source_ids),
            "preserved_frozen_charges": sum(
                int(source.execute(
                    "SELECT COUNT(*) FROM billing_period_charges "
                    "WHERE account_identity_id=?", (account_id,)
                ).fetchone()[0]) for account_id in source_ids
            ),
        }
    finally:
        shadow.close()
        source.close()
