"""Small, transaction-local helpers for deleting live routing resources.

Historical request rows deliberately keep their scalar identifiers, but no
longer retain foreign-key ownership of a resource that has been removed.
Callers own the transaction and decide whether the operation is part of a
larger lifecycle change.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


def _ids(values: Iterable[int]) -> list[int]:
    return sorted({int(value) for value in values})


def _placeholders(values: list[int]) -> str:
    return ",".join("?" for _ in values)


def purge_client_keys(conn: sqlite3.Connection,
                      key_ids: Iterable[int]) -> int:
    """Detach request history, then physically delete client keys."""
    ids = _ids(key_ids)
    if not ids:
        return 0
    placeholders = _placeholders(ids)
    conn.execute(
        f"UPDATE request_log SET client_key_id=NULL "
        f"WHERE client_key_id IN ({placeholders})", ids)
    deleted = conn.execute(
        f"DELETE FROM client_keys WHERE id IN ({placeholders})", ids)
    return max(deleted.rowcount, 0)


def purge_route_sets(conn: sqlite3.Connection,
                     route_set_ids: Iterable[int], *,
                     purge_keys: bool = True) -> int:
    """Delete route rules/sets after detaching historical request rows.

    Aggregate route sets cannot leave their client keys attached to a missing
    route set.  The default therefore purges those keys as well; callers that
    have already moved keys elsewhere can pass ``purge_keys=False``.
    """
    ids = _ids(route_set_ids)
    if not ids:
        return 0
    placeholders = _placeholders(ids)
    conn.execute(
        f"UPDATE request_log SET route_set_id=NULL "
        f"WHERE route_set_id IN ({placeholders})", ids)
    deleted = 0
    if purge_keys:
        key_rows = conn.execute(
            f"SELECT id FROM client_keys WHERE route_set_id IN ({placeholders})",
            ids).fetchall()
        deleted += purge_client_keys(conn, [row[0] for row in key_rows])
    conn.execute(
        f"DELETE FROM route_rules WHERE route_set_id IN ({placeholders})", ids)
    removed = conn.execute(
        f"DELETE FROM route_sets WHERE id IN ({placeholders})", ids)
    return deleted + max(removed.rowcount, 0)


def purge_expired_secrets(conn: sqlite3.Connection, now: str) -> int:
    """V2 compatibility hook; paused credentials retain their secrets."""
    del conn, now
    return 0


def purge_credential(conn: sqlite3.Connection, credential_uuid: str) -> int:
    """Physically delete one credential while detaching historical FKs."""
    conn.execute("UPDATE request_log SET credential_uuid=NULL,upstream_key_id=NULL "
                 "WHERE credential_uuid=?", (credential_uuid,))
    conn.execute("UPDATE request_attempts SET credential_uuid=NULL,upstream_key_id=NULL "
                 "WHERE credential_uuid=?", (credential_uuid,))
    conn.execute("DELETE FROM upstream_secrets WHERE credential_uuid=?",
                 (credential_uuid,))
    return max(conn.execute("DELETE FROM upstream_credentials WHERE uuid=?",
                           (credential_uuid,)).rowcount, 0)


def purge_proxy_account(conn: sqlite3.Connection,
                        account_id: int) -> bool:
    """Remove a proxy live resource while retaining identity and ledger rows.

    Request history is retained through ``account_identity_id``; only the
    live account/contract/credential graph is removed.  Frozen charge rows
    carry source IDs/snapshots but no longer have parent FKs (V1.17).
    """
    # Remove the account-owned routing graph first.  Aggregate route rules can
    # also point at this account's upstreams, so those rules must be removed
    # before the upstream rows themselves are deleted; otherwise the live
    # configuration would fail its foreign-key check while frozen ledgers are
    # still safely decoupled from the deleted contract.
    route_set_ids = [row[0] for row in conn.execute(
        "SELECT id FROM route_sets WHERE account_id=?", (account_id,)
    ).fetchall()]
    purge_route_sets(conn, route_set_ids)
    conn.execute(
        "DELETE FROM route_rules WHERE upstream_id IN "
        "(SELECT id FROM upstreams WHERE account_id=?)", (account_id,))

    conn.execute(
        "UPDATE request_log SET account_identity_id=COALESCE(account_identity_id,account_id),"
        "account_id=NULL,route_set_id=NULL,client_key_id=NULL,"
        "credential_uuid=NULL,upstream_key_id=NULL WHERE account_id=?", (account_id,))
    conn.execute(
        "UPDATE request_attempts SET upstream_id=NULL,credential_uuid=NULL "
        "WHERE account_id=? OR upstream_id IN "
        "(SELECT id FROM upstreams WHERE account_id=?)", (account_id, account_id))
    conn.execute(
        "UPDATE request_log SET credential_uuid=NULL,upstream_key_id=NULL "
        "WHERE credential_uuid IN (SELECT c.uuid FROM upstream_credentials c "
        "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?)",
        (account_id,))
    conn.execute(
        "UPDATE request_attempts SET credential_uuid=NULL,upstream_key_id=NULL "
        "WHERE credential_uuid IN (SELECT c.uuid FROM upstream_credentials c "
        "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?)",
        (account_id,))
    conn.execute(
        "DELETE FROM upstream_secrets WHERE credential_uuid IN "
        "(SELECT c.uuid FROM upstream_credentials c JOIN upstreams u "
        "ON u.id=c.upstream_id WHERE u.account_id=?)", (account_id,))
    conn.execute(
        "DELETE FROM upstream_credentials WHERE upstream_id IN "
        "(SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
    conn.execute(
        "DELETE FROM upstream_model_catalog WHERE upstream_id IN "
        "(SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
    conn.execute(
        "DELETE FROM billing_rate_events WHERE contract_id IN "
        "(SELECT id FROM billing_contracts WHERE account_id=?)", (account_id,))
    conn.execute("DELETE FROM account_importers WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM billing_contracts WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM upstreams WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return True


def purge_agent_subscription_instance(conn: sqlite3.Connection,
                                      instance_id: int) -> bool:
    """Remove one live Agent instance while retaining its identity/charges.

    ``agent_subscription_period_charges`` stores the instance id as a scalar
    historical identity after V1.17; it deliberately has no FK to this live
    row.  Rate events are live pricing configuration and can therefore be
    removed before the instance itself.
    """
    row = conn.execute(
        "SELECT id FROM agent_subscription_instances WHERE id=?",
        (instance_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "DELETE FROM agent_subscription_rate_events WHERE instance_id=?",
        (instance_id,),
    )
    conn.execute(
        "DELETE FROM agent_subscription_instances WHERE id=?",
        (instance_id,),
    )
    return True


def purge_agent_subscription(conn: sqlite3.Connection,
                              subscription_id: int) -> bool:
    """Remove an Agent subscription's live graph, retaining history.

    The identity tables are intentionally not touched.  Frozen period charges,
    charge allocations and immutable export events refer to those identities
    and remain valid after the live subscription, instances, rates and
    software bindings are physically removed.
    """
    row = conn.execute(
        "SELECT id FROM agent_subscriptions WHERE id=?",
        (subscription_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "DELETE FROM agent_subscription_bindings WHERE subscription_id=?",
        (subscription_id,),
    )
    conn.execute(
        "DELETE FROM agent_subscription_rate_events WHERE instance_id IN "
        "(SELECT id FROM agent_subscription_instances WHERE subscription_id=?)",
        (subscription_id,),
    )
    conn.execute(
        "DELETE FROM agent_subscription_instances WHERE subscription_id=?",
        (subscription_id,),
    )
    conn.execute(
        "DELETE FROM agent_subscriptions WHERE id=?",
        (subscription_id,),
    )
    return True
