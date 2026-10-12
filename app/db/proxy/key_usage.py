"""Read/write helpers for the per-credential upstream usage snapshot.

``upstream_credentials.usage_json`` holds the last observed usage windows of
one upstream key.  The poller reads the target upstreams and their plaintext
keys with one connection, performs the HTTP queries with no database lock
held, then commits every payload in a single runtime write transaction.  The
existing ``config_credentials_au`` trigger advances ``config_state`` on that
update, so the column is written like any other credential attribute.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.core import sqlite_runtime
from app.core.time import format_utc, utc_now
from app.domain.account_types import ACCOUNT_TYPES


def usage_path(subtype: str | None) -> str | None:
    """Usage query path declared by the spec of *subtype* (None when absent)."""
    if not subtype:
        return None
    for type_spec in ACCOUNT_TYPES.values():
        sub = type_spec.subtypes.get(subtype)
        if sub is not None and sub.usage_path:
            return sub.usage_path
    return None


def usage_targets(conn: sqlite3.Connection, *, moment=None) -> list[dict]:
    """Enabled proxy upstreams whose subtype declares a usage query path.

    Only credentials with a stored plaintext and no scheduled end are
    returned: a key without a secret cannot be queried and an ended key is
    already on its way out of routing.
    """
    now = format_utc(moment or utc_now())
    rows = conn.execute(
        "SELECT u.id upstream_id,u.account_id,u.base_url,u.subtype "
        "FROM upstreams u JOIN accounts a ON a.id=u.account_id "
        "WHERE a.account_kind='proxy' AND u.enabled=1 AND u.subtype IS NOT NULL "
        "ORDER BY u.id",
    ).fetchall()
    targets: list[dict] = []
    for row in rows:
        path = usage_path(row["subtype"])
        base_url = str(row["base_url"] or "").strip()
        if not path or not base_url:
            continue
        credentials = conn.execute(
            "SELECT c.uuid,s.secret_value,c.usage_json "
            "FROM upstream_credentials c JOIN upstream_secrets s "
            "ON s.credential_uuid=c.uuid "
            "WHERE c.upstream_id=? AND c.enabled=1 AND s.secret_value IS NOT NULL "
            "AND (c.ends_at IS NULL OR c.ends_at>?) "
            "ORDER BY c.position,c.runtime_id",
            (row["upstream_id"], now),
        ).fetchall()
        if not credentials:
            continue
        targets.append({
            "account_id": int(row["account_id"]),
            "upstream_id": int(row["upstream_id"]),
            "url": base_url.rstrip("/") + path,
            "credentials": [dict(credential) for credential in credentials],
        })
    return targets


def record_usage_snapshots(db_path: str, snapshots: Mapping[str, dict]) -> int:
    """Commit usage payloads for credential UUIDs in one write transaction."""
    if not snapshots:
        return 0
    conn = sqlite_runtime.connect(db_path, "proxy_runtime")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            conn.executemany(
                "UPDATE upstream_credentials SET usage_json=? WHERE uuid=?",
                [(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                  uuid) for uuid, payload in snapshots.items()],
            )
        return len(snapshots)
    finally:
        conn.close()


__all__ = ["record_usage_snapshots", "usage_path", "usage_targets"]
