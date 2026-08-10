"""Functional WebDAV synchronization module."""

from app.services.sync.common import *  # noqa: F401,F403
from app.services.sync.settings import SyncConfig

def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _config_tables(conn: sqlite3.Connection) -> list[str]:
    return V1_CONFIG_TABLES


def _config_hash_of_db(db_path: str) -> str:
    """Hash the V1 cloud-representation for conflict detection."""
    conn = sqlite3.connect(db_path)
    h = hashlib.sha256()
    try:
        for table in _config_tables(conn):
            if not _table_exists(conn, table):
                continue
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
            if table == "upstream_credentials":
                cols = [column for column in cols if column != "runtime_id"]
            elif table == "account_importers":
                cols = [column for column in cols if column != "cursor_json"]
            if not cols:
                continue
            h.update(table.encode())
            rows = conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY 1").fetchall()
            for r in rows:
                h.update(repr(tuple(r)).encode())
    finally:
        conn.close()
    return h.hexdigest()


def _get_sync_state(db_path: str, key: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_sync_state(db_path: str, key: str, value: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?,?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def record_remote_metadata(db_path: str, prefix: str, sha256: str,
                           major: int | None, minor: int | None) -> None:
    """Persist the authoritative cloud artifact's identity in sync_state.

    Alongside the canonical config hash and artifact/etag, keep the raw
    sha256 of the exact cloud file and its schema major.minor so a restarted
    node can judge remote compatibility (e.g. a higher minor → read-compatible
    / write-paused) without re-downloading or re-deriving it.
    """
    _set_sync_state(db_path, f"{prefix}_remote_sha256", sha256)
    if major is not None:
        _set_sync_state(db_path, f"{prefix}_remote_major", str(major))
        _set_sync_state(db_path, f"{prefix}_remote_minor", str(minor))


def _snapshot_path(db_path: str) -> str:
    """Path to the local V1 configuration snapshot used by discard."""
    return str(Path(db_path).resolve().parent / "config_snapshot.db")


def snapshot_config(db_path: str) -> None:
    """Copy V1 metadata and local credential secrets to the discard snapshot."""
    snap = _snapshot_path(db_path)
    _safe_copy_db(db_path, snap)
    snapshot = sqlite3.connect(snap)
    try:
        for table in ("request_attempts", "request_log", "billing_period_charges", "fx_rates"):
            if _table_exists(snapshot, table):
                snapshot.execute(f"DELETE FROM {table}")
        snapshot.commit()
    finally:
        snapshot.close()


def restore_config_snapshot(db_path: str) -> bool:
    """Restore V1 metadata and secrets from the last local snapshot."""
    snap_path = _snapshot_path(db_path)
    if not os.path.exists(snap_path):
        return False
    snapshot = sqlite3.connect(snap_path)
    snapshot.row_factory = sqlite3.Row
    local = sqlite3.connect(db_path, timeout=10)
    try:
        local.execute("PRAGMA foreign_keys=OFF")
        local.execute("BEGIN IMMEDIATE")
        for table in V1_CONFIG_TABLES + ["upstream_secrets"]:
            if not _table_exists(snapshot, table) or not _table_exists(local, table):
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
