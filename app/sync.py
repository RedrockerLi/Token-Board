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
    base_url: str     # e.g. "https://dav.example.com/remote.php/dav/files/user"
    folder: str       # e.g. "token-board-sync" — subfolder to store the DB in
    username: str
    password: str

    @property
    def full_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.folder.strip('/')}/proxy_sync.db"


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
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('url', ?)", (config.base_url,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('folder', ?)", (config.folder,))
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
            old_url = rows["url"]

            # If the old url is just a scheme or malformed, use it as-is with a default folder
            if old_url.count("/") < 3:
                # Probably just a base URL like "https://dav.jianguoyun.com/dav"
                base = old_url.rstrip("/")
                folder = rows.get("folder", "token-board-sync")
            else:
                # Full path like "https://dav.jianguoyun.com/dav/folder/file.db"
                # Strip last two segments (folder/file.db) to get base
                parts = old_url.rstrip("/").rsplit("/", 2)
                if len(parts) >= 3:
                    base = parts[0]
                    folder = parts[1] if not "." in parts[1] else rows.get("folder", "token-board-sync")
                    # Don't lose the actual base if we stripped too much
                    if not base.startswith("https://") and not base.startswith("http://"):
                        base = old_url.rstrip("/")
                        folder = rows.get("folder", "token-board-sync")
                else:
                    base = parts[0]
                    folder = rows.get("folder", "token-board-sync")
            folder = rows.get("folder", folder)  # explicit folder takes priority
            return SyncConfig(
                base_url=base,
                folder=folder,
                username=rows["username"],
                password=rows.get("password", ""),
            )
        return None
    finally:
        conn.close()


# ── Minimal WebDAV client ─────────────────────────────────────────────────

class WebDAVError(Exception):
    pass


def _webdav_download(config: SyncConfig, dest_path: str) -> bool:
    """Download remote DB. Returns True if downloaded, False if not found (first sync)."""
    resp = requests.get(
        config.full_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=30,
    )
    if resp.status_code in (404, 410):
        return False  # no remote DB yet — first sync
    if not resp.ok:
        raise WebDAVError(f"Download failed: HTTP {resp.status_code}")
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return True


def _webdav_ensure_folder(config: SyncConfig):
    """Ensure the target folder exists on the WebDAV server (create if needed)."""
    folder_url = config.full_url.rsplit("/", 1)[0] + "/"
    resp = requests.request(
        "PROPFIND", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=10,
    )
    if resp.status_code in (404, 410):
        # Folder doesn't exist — create it
        resp = requests.request(
            "MKCOL", folder_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if not resp.ok and resp.status_code != 405:
            raise WebDAVError(f"无法创建文件夹 {folder_url}: HTTP {resp.status_code}")
    elif not resp.ok:
        raise WebDAVError(f"无法访问文件夹 {folder_url}: HTTP {resp.status_code}")


def _webdav_upload(config: SyncConfig, src_path: str):
    """Upload a file to WebDAV from src_path."""
    _webdav_ensure_folder(config)
    with open(src_path, "rb") as f:
        resp = requests.put(
            config.full_url,
            data=f,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=60,
        )
    if not resp.ok:
        raise WebDAVError(f"Upload failed: HTTP {resp.status_code}")


def _webdav_test(config: SyncConfig) -> str | None:
    """Test WebDAV connectivity. Returns None on success, error string on failure."""
    try:
        folder_url = config.full_url.rsplit("/", 1)[0] + "/"

        # 1. Try PROPFIND on the folder
        resp = requests.request(
            "PROPFIND", folder_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.ok:
            return None  # folder exists, connection OK

        # 2. Folder doesn't exist — try to create it with MKCOL
        if resp.status_code in (404, 410):
            resp = requests.request(
                "MKCOL", folder_url,
                auth=HTTPBasicAuth(config.username, config.password),
                timeout=10,
            )
            if resp.ok or resp.status_code == 405:  # 405 = already exists (some servers)
                return None
            return f"无法创建文件夹: HTTP {resp.status_code} — {resp.text[:200]}"

        # 3. Auth error?
        if resp.status_code == 401 or resp.status_code == 403:
            return f"认证失败 (HTTP {resp.status_code}) — 请检查用户名和密码。坚果云需要使用「应用密码」而非账号密码"

        # 4. Other error — try simple GET on the file
        resp = requests.get(
            config.full_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.status_code in (200, 404, 410):
            return None
        return f"连接失败: HTTP {resp.status_code} — {resp.text[:200]}"
    except requests.RequestException as e:
        return f"网络错误: {e}"


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
        # 1. Download remote DB (skip merge if first sync)
        has_remote = _webdav_download(config, remote_path)
        new_count = 0

        if has_remote:
            # 2. Ensure remote DB has the correct schema
            remote_conn = sqlite3.connect(remote_path)
            _ensure_schema(remote_conn)
            remote_conn.close()

            # 3. Open local DB
            local_conn = sqlite3.connect(db_path)
            local_conn.row_factory = sqlite3.Row

            # 4. Merge remote request_log into local
            remote_conn = sqlite3.connect(remote_path)
            remote_conn.row_factory = sqlite3.Row
            new_count = _merge_request_log(local_conn, remote_conn)
            remote_conn.close()

            # 5. Close local
            local_conn.close()

        # 6. Create trimmed copy for upload (30 days, usage only — no keys)
        shutil.copy(db_path, merged_path)
        merged_conn = sqlite3.connect(merged_path)
        _trim_old(merged_conn)
        merged_conn.execute("DELETE FROM upstream_accounts")
        merged_conn.execute("DELETE FROM local_keys")
        merged_conn.execute("DELETE FROM sync_config")
        merged_conn.commit()
        merged_conn.close()

        # 7. Upload
        _webdav_upload(config, merged_path)

        if has_remote:
            msg = f"同步成功，从远端合并了 {new_count} 条新记录"
        else:
            msg = "首次同步成功，已将本地数据上传至云端"
        return {
            "status": "ok",
            "message": msg,
            "remote_records": new_count,
        }

    except WebDAVError as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": f"WebDAV 错误: {e}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": f"同步失败: {type(e).__name__}: {e}"}
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
