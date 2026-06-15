"""WebDAV-based database sync for multi-machine proxy usage.

Flow:
  1. Pull remote DB from WebDAV → tmp/remote.db
  2. Merge remote request_log into local (one-way, dedup by content hash)
  3. Create trimmed copy (30 days, usage only — no keys/config)
  4. Push trimmed copy → WebDAV
  5. Cleanup tmp/

Local DB is the complete source of truth. Cloud only holds a 30-day
usage snapshot (request_log + model_pricing, no credentials).

All temp files live in a tmp/ directory under the project root,
created on demand and removed after sync.
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth


@dataclass
class SyncConfig:
    """WebDAV connection parameters."""
    url: str          # e.g. "https://dav.example.com/remote.php/dav/files/user/proxy.db"
    username: str
    password: str


# ── Config persistence ────────────────────────────────────────────────────

def _ensure_sync_config_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()


def save_sync_config(db_path: str, config: SyncConfig):
    conn = sqlite3.connect(db_path)
    try:
        _ensure_sync_config_table(conn)
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('url', ?)", (config.url,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('username', ?)", (config.username,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('password', ?)", (config.password,))
        conn.commit()
    finally:
        conn.close()


def load_sync_config(db_path: str) -> SyncConfig | None:
    conn = sqlite3.connect(db_path)
    try:
        _ensure_sync_config_table(conn)
        rows = dict(conn.execute("SELECT key, value FROM sync_config").fetchall())
        if rows.get("url") and rows.get("username"):
            return SyncConfig(
                url=rows["url"],
                username=rows["username"],
                password=rows.get("password", ""),
            )
        return None
    finally:
        conn.close()


# ── Minimal WebDAV client ─────────────────────────────────────────────────

class WebDAVError(Exception):
    pass


def _webdav_download(config: SyncConfig, dest_path: str):
    """Download a file from WebDAV to dest_path."""
    resp = requests.get(
        config.url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=30,
    )
    if resp.status_code == 404:
        # No remote DB yet — create an empty one
        conn = sqlite3.connect(dest_path)
        conn.close()
        return
    if not resp.ok:
        raise WebDAVError(f"Download failed: HTTP {resp.status_code}")
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def _webdav_upload(config: SyncConfig, src_path: str):
    """Upload a file to WebDAV from src_path."""
    with open(src_path, "rb") as f:
        resp = requests.put(
            config.url,
            data=f,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=60,
        )
    if not resp.ok:
        raise WebDAVError(f"Upload failed: HTTP {resp.status_code}")


def _webdav_test(config: SyncConfig) -> str | None:
    """Test WebDAV connectivity. Returns None on success, error string on failure."""
    try:
        # Try PROPFIND to check connection
        resp = requests.request(
            "PROPFIND",
            config.url.rsplit("/", 1)[0] + "/",  # parent directory
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.ok:
            return None
        # Some servers don't support PROPFIND, try GET on the file itself
        resp = requests.get(
            config.url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.status_code in (200, 404):
            return None
        return f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return str(e)


# ── Merge logic ───────────────────────────────────────────────────────────

def _record_key(row: dict) -> str:
    """Deterministic dedup key for a request_log record."""
    parts = (
        str(row.get("account_id", 0)),
        str(row.get("local_key_id", 0)),
        str(row.get("model", "")),
        str(row.get("prompt_tokens", 0)),
        str(row.get("completion_tokens", 0)),
        str(row.get("total_tokens", 0)),
        str(row.get("requested_at", "")),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _merge_request_log(local_conn: sqlite3.Connection, remote_conn: sqlite3.Connection):
    """Merge remote request_log into local, deduplicating by content hash."""
    local_conn.row_factory = sqlite3.Row
    remote_conn.row_factory = sqlite3.Row

    cutoff = "date('now', '-30 days')"

    # Read existing records from local (within 30 days)
    local_rows = local_conn.execute(
        f"SELECT * FROM request_log WHERE date(requested_at) >= {cutoff}"
    ).fetchall()

    # Build set of local record keys
    local_keys = {_record_key(dict(r)) for r in local_rows}

    # Read remote records
    remote_rows = remote_conn.execute(
        f"SELECT * FROM request_log WHERE date(requested_at) >= {cutoff}"
    ).fetchall()

    # Insert remote records missing from local (one-way: remote → local)
    new_count = 0
    for r in remote_rows:
        rd = dict(r)
        if _record_key(rd) not in local_keys:
            local_conn.execute(
                """INSERT INTO request_log
                   (account_id, local_key_id, model, prompt_tokens,
                    completion_tokens, total_tokens, cost, is_streaming,
                    status_code, duration_ms, requested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rd["account_id"], rd["local_key_id"], rd["model"],
                    rd["prompt_tokens"], rd["completion_tokens"], rd["total_tokens"],
                    rd["cost"], rd["is_streaming"], rd["status_code"],
                    rd["duration_ms"], rd["requested_at"],
                ),
            )
            new_count += 1

    local_conn.commit()
    return new_count


def _merge_config_tables(local_conn: sqlite3.Connection, remote_conn: sqlite3.Connection):
    """Merge upstream_accounts, local_keys, model_pricing from remote into local.

    Uses INSERT OR IGNORE based on unique constraints (name, key_value, model_pattern).
    """
    # upstream_accounts: unique on (name)
    local_conn.execute("""
        INSERT OR IGNORE INTO upstream_accounts (name, upstream_key, base_url, created_at)
        SELECT name, upstream_key, base_url, created_at
        FROM temp_remote.upstream_accounts
    """)

    # local_keys: unique on (key_value)
    local_conn.execute("""
        INSERT OR IGNORE INTO local_keys (key_value, label, account_id, created_at, last_used_at)
        SELECT key_value, label, account_id, created_at, last_used_at
        FROM temp_remote.local_keys
    """)

    # model_pricing: unique on (model_pattern)
    local_conn.execute("""
        INSERT OR IGNORE INTO model_pricing (model_pattern, input_price, output_price, currency)
        SELECT model_pattern, input_price, output_price, currency
        FROM temp_remote.model_pricing
    """)

    local_conn.commit()


def _trim_old(conn: sqlite3.Connection):
    """Delete request_log records older than 30 days."""
    conn.execute("DELETE FROM request_log WHERE date(requested_at) < date('now', '-30 days')")
    conn.commit()


# ── Main sync entry point ─────────────────────────────────────────────────

def sync(db_path: str) -> dict:
    """Execute a full bidirectional sync.

    Returns:
        dict with keys: status ('ok'|'error'), message, remote_records (int)
    """
    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)

    remote_path = str(tmp_dir / "remote.db")
    merged_path = str(tmp_dir / "merged.db")

    config = load_sync_config(db_path)
    if not config:
        return {"status": "error", "message": "未配置同步服务器，请先点击齿轮图标设置"}

    try:
        # 1. Download remote DB
        _webdav_download(config, remote_path)

        # 2. Ensure remote DB has the correct schema
        remote_conn = sqlite3.connect(remote_path)
        _ensure_schema(remote_conn)
        remote_conn.close()

        # 3. Open local DB
        local_conn = sqlite3.connect(db_path)
        local_conn.row_factory = sqlite3.Row

        # 4. Merge remote request_log into local (one-way: remote → local)
        remote_conn = sqlite3.connect(remote_path)
        remote_conn.row_factory = sqlite3.Row
        new_count = _merge_request_log(local_conn, remote_conn)
        remote_conn.close()

        # 5. Local DB stays complete — close without trimming
        local_conn.close()

        # 6. Create trimmed copy for upload (30 days, usage only — no keys)
        shutil.copy(db_path, merged_path)
        merged_conn = sqlite3.connect(merged_path)
        _trim_old(merged_conn)
        # Strip all sensitive data before uploading
        merged_conn.execute("DELETE FROM upstream_accounts")
        merged_conn.execute("DELETE FROM local_keys")
        merged_conn.execute("DELETE FROM sync_config")
        merged_conn.commit()
        merged_conn.close()

        # 7. Upload trimmed copy
        _webdav_upload(config, merged_path)

        return {
            "status": "ok",
            "message": f"同步成功，从远端合并了 {new_count} 条新记录",
            "remote_records": new_count,
        }

    except WebDAVError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": f"同步失败: {e}"}
    finally:
        # Cleanup
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _ensure_schema(conn: sqlite3.Connection):
    """Ensure the remote DB has the same tables as local."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS upstream_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            upstream_key TEXT NOT NULL,
            base_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS local_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value TEXT NOT NULL UNIQUE,
            label TEXT,
            account_id INTEGER NOT NULL REFERENCES upstream_accounts(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            local_key_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0.0,
            is_streaming INTEGER NOT NULL DEFAULT 0,
            status_code INTEGER NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            requested_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_rl_account ON request_log(account_id);
        CREATE INDEX IF NOT EXISTS idx_rl_time ON request_log(requested_at);
        CREATE TABLE IF NOT EXISTS model_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_pattern TEXT NOT NULL UNIQUE,
            input_price REAL NOT NULL,
            output_price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY'
        );
        CREATE TABLE IF NOT EXISTS proxy_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sync_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
