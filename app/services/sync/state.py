"""Small, explicit sync-state and sanitized-config persistence boundary."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping

from app.core import sqlite_runtime
from app.services.sync.common import V1_CONFIG_TABLES


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def config_hash_of_db(db_path: str) -> str:
    """Hash the allowlisted, sanitized cloud representation."""
    conn = sqlite_runtime.connect(db_path, "shadow_copy")
    digest = hashlib.sha256()
    try:
        for table in V1_CONFIG_TABLES:
            if not table_exists(conn, table) or table == "upstream_secrets":
                continue
            columns = [row[0] for row in conn.execute(
                f"SELECT * FROM {table} LIMIT 0").description]
            if table == "upstream_credentials":
                columns = [column for column in columns if column != "runtime_id"]
            elif table == "account_importers":
                columns = [column for column in columns if column != "cursor_json"]
            if not columns:
                continue
            digest.update(table.encode())
            if table == "sync_settings":
                rows = conn.execute(
                    f"SELECT {','.join(columns)} FROM {table} "
                    "WHERE key NOT IN ('password','agent_migration_v1_6') ORDER BY 1"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {','.join(columns)} FROM {table} ORDER BY 1"
                ).fetchall()
            for row in rows:
                digest.update(repr(tuple(row)).encode())
    finally:
        conn.close()
    return digest.hexdigest()


def get_sync_state(db_path: str, key: str) -> str | None:
    conn = sqlite_runtime.connect(db_path, "proxy_runtime")
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_sync_state(db_path: str, key: str, value: str) -> None:
    set_sync_state_many(db_path, {key: value})


def set_sync_state_many(db_path: str, values: Mapping[str, str]) -> None:
    """Commit a group of recovery markers in one runtime transaction."""
    if not values:
        return
    conn = sqlite_runtime.connect(db_path, "proxy_runtime")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            conn.executemany(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?,?)",
                [(str(key), str(value)) for key, value in values.items()],
            )
    finally:
        conn.close()


def clear_sync_state(db_path: str, key: str) -> None:
    clear_sync_state_many(db_path, (key,))


def clear_sync_state_many(db_path: str, keys: Iterable[str]) -> None:
    """Clear a group of recovery markers in one runtime transaction."""
    keys = tuple(str(key) for key in keys)
    if not keys:
        return
    conn = sqlite_runtime.connect(db_path, "proxy_runtime")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            conn.executemany("DELETE FROM sync_state WHERE key=?",
                             [(key,) for key in keys])
    finally:
        conn.close()


def record_remote_metadata(db_path: str, prefix: str, sha256: str,
                           major: int | None, minor: int | None) -> None:
    set_sync_state(db_path, f"{prefix}_remote_sha256", sha256)
    if major is not None:
        set_sync_state(db_path, f"{prefix}_remote_major", str(major))
        set_sync_state(db_path, f"{prefix}_remote_minor", str(minor))
