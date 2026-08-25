"""Configuration snapshot and restore boundary."""

import os
import sqlite3
from pathlib import Path

from app.core import sqlite_runtime
from app.services.sync.common import V1_CONFIG_TABLES
from app.services.sync.state import table_exists
from app.services.sync.storage import safe_copy_db


def _snapshot_path(db_path: str) -> str:
    """Path to the local V1 configuration snapshot used by discard."""
    return str(Path(db_path).resolve().parent / "token-board_config_snapshot.db")


def snapshot_config(db_path: str) -> None:
    """Copy V1 metadata and local credential secrets to the discard snapshot."""
    snap = _snapshot_path(db_path)
    safe_copy_db(db_path, snap)
    snapshot = sqlite_runtime.connect(snap, "snapshot_restore")
    try:
        for table in ("request_attempts", "request_log", "billing_period_charges",
                      "agent_subscription_period_charges", "agent_software_runtime",
                      "fx_rates", "sync_state"):
            if table_exists(snapshot, table):
                snapshot.execute(f"DELETE FROM {table}")
        snapshot.commit()
    finally:
        snapshot.close()


def restore_config_snapshot(db_path: str) -> bool:
    """Restore V1 metadata and secrets from the last local snapshot."""
    snap_path = _snapshot_path(db_path)
    if not os.path.exists(snap_path):
        return False
    snapshot = sqlite_runtime.connect(snap_path, "snapshot_restore")
    local = sqlite_runtime.connect(db_path, "proxy_runtime")
    try:
        local.execute("PRAGMA foreign_keys=OFF")
        local.execute("BEGIN IMMEDIATE")
        for table in V1_CONFIG_TABLES:
            if not table_exists(snapshot, table) or not table_exists(local, table):
                continue
            info = [row[1] for row in snapshot.execute(f"PRAGMA table_info({table})")]
            if not info:
                continue
            local.execute(f"DELETE FROM {table}")
            rows = snapshot.execute(f"SELECT {','.join(info)} FROM {table}").fetchall()
            if rows:
                placeholders = ",".join("?" for _ in info)
                local.executemany(
                    f"INSERT INTO {table}({','.join(info)}) VALUES({placeholders})",
                    [tuple(row) for row in rows],
                )
        violation = local.execute("PRAGMA foreign_key_check").fetchone()
        if violation is not None:
            raise sqlite3.IntegrityError(f"V1 snapshot restore FK violation: {tuple(violation)}")
        local.commit()
        local.execute("PRAGMA foreign_keys=ON")
        return True
    except Exception:
        local.rollback()
        raise
    finally:
        snapshot.close()
        local.close()
