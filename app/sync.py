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
    folder: str       # e.g. "token-board-sync" — subfolder to store files in
    username: str
    password: str
    filename: str = "proxy_sync.db"  # default filename for proxy DB

    @property
    def full_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.folder.strip('/')}/{self.filename}"


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


def _build_url(config: SyncConfig, remote_filename: str | None = None) -> str:
    """Build the full WebDAV URL. Override filename if provided."""
    fn = remote_filename or config.filename
    return f"{config.base_url.rstrip('/')}/{config.folder.strip('/')}/{fn}"


def _webdav_download(config: SyncConfig, dest_path: str, remote_filename: str | None = None) -> bool:
    """Download remote DB. Returns True if downloaded, False if not found (first sync)."""
    url = _build_url(config, remote_filename)
    resp = requests.get(
        url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=30,
    )
    if resp.status_code in (404, 409, 410):
        return False  # no remote DB yet — first sync
    if not resp.ok:
        raise WebDAVError(f"Download failed: HTTP {resp.status_code}")
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return True


def _webdav_ensure_folder(config: SyncConfig, remote_filename: str | None = None):
    """Ensure the target folder exists on the WebDAV server (create if needed)."""
    url = _build_url(config, remote_filename)
    folder_url = url.rsplit("/", 1)[0] + "/"
    # Try MKCOL first — succeeds if folder is created, returns 405 if already exists
    resp = requests.request(
        "MKCOL", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=10,
    )
    if resp.ok or resp.status_code in (405, 409):  # 405=exists, 409=坚果云
        return
    # MKCOL failed — try PROPFIND to check
    resp2 = requests.request(
        "PROPFIND", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=10,
    )
    if resp2.ok:
        return
    raise WebDAVError(f"无法创建/访问文件夹 {folder_url}: HTTP {resp.status_code}/{resp2.status_code}")


def _webdav_upload(config: SyncConfig, src_path: str, remote_filename: str | None = None):
    """Upload a file to WebDAV from src_path."""
    _webdav_ensure_folder(config, remote_filename)
    url = _build_url(config, remote_filename)
    # Attempt PUT directly; if it fails with 404/409, folder issue already caught above
    with open(src_path, "rb") as f:
        resp = requests.put(
            url,
            data=f,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=60,
        )
    if not resp.ok:
        raise WebDAVError(f"Upload failed: HTTP {resp.status_code} — {resp.text[:200]}")


def _webdav_test(config: SyncConfig) -> str | None:
    """Test WebDAV connectivity. Returns None on success, error string on failure."""
    try:
        folder_url = config.full_url.rsplit("/", 1)[0] + "/"

        # 1. Try MKCOL — works on most servers, returns 405 if folder exists
        resp = requests.request(
            "MKCOL", folder_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.ok or resp.status_code in (405, 409):
            return None

        # 2. Check for auth errors
        if resp.status_code in (401, 403):
            return f"认证失败 (HTTP {resp.status_code}) — 坚果云请使用「应用密码」"

        # 3. Fallback: try GET on the file
        resp = requests.get(
            config.full_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.status_code in (200, 404, 409, 410):
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


def _recalculate_all_costs(conn: sqlite3.Connection):
    """Recalculate cost for all request_log entries using current pricing."""
    pricing = conn.execute(
        "SELECT model_pattern, input_price, output_price FROM model_pricing ORDER BY id"
    ).fetchall()
    if not pricing:
        return

    # Build a list of (pattern, input_price, output_price) and match each model
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM request_log").fetchall()]
    for model in models:
        for pattern, inp, out in pricing:
            if _simple_glob(pattern, model):
                conn.execute(
                    """UPDATE request_log
                       SET cost = (prompt_tokens / 1000000.0) * ? + (completion_tokens / 1000000.0) * ?
                       WHERE model = ?""",
                    (inp, out, model),
                )
                break  # first match wins
    conn.commit()


def _simple_glob(pattern: str, text: str) -> bool:
    """Simple glob match (* and ? only)."""
    pi = mi = 0
    star = -1
    match_start = 0
    while mi < len(text):
        if pi < len(pattern) and (pattern[pi] == '?' or
                                   pattern[pi].lower() == text[mi].lower()):
            pi += 1; mi += 1
        elif pi < len(pattern) and pattern[pi] == '*':
            star = pi; match_start = mi; pi += 1
        elif star != -1:
            pi = star + 1; match_start += 1; mi = match_start
        else:
            return False
    while pi < len(pattern) and pattern[pi] == '*':
        pi += 1
    return pi == len(pattern)


def _safe_copy_db(src: str, dst: str):
    """Copy a SQLite database, including WAL data, using the backup API."""
    src_conn = sqlite3.connect(src)
    src_conn.execute("PRAGMA busy_timeout=5000")
    # Force WAL checkpoint so all data is in the main file
    src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dst_conn = sqlite3.connect(dst)
    dst_conn.execute("PRAGMA busy_timeout=5000")
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()


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

            # 3. Merge remote request_log into local
            local_conn = sqlite3.connect(db_path, timeout=10)
            local_conn.row_factory = sqlite3.Row
            local_conn.execute("PRAGMA busy_timeout=5000")

            remote_conn = sqlite3.connect(remote_path)
            remote_conn.row_factory = sqlite3.Row
            new_count = _merge_request_log(local_conn, remote_conn)
            remote_conn.close()
            local_conn.close()

        # 4. Create trimmed copy for upload (request_log only, 30 days, no config/secrets)
        _safe_copy_db(db_path, merged_path)
        merged_conn = sqlite3.connect(merged_path)
        _trim_old(merged_conn)
        # Strip all config/secrets — only request_log goes to cloud
        for tbl in CONFIG_TABLES + ["sync_config", "perf_events"]:
            merged_conn.execute(f"DELETE FROM {tbl}")
        merged_conn.commit()
        merged_conn.close()

        # 5. Count and upload
        upload_conn = sqlite3.connect(merged_path)
        uploaded_count = upload_conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
        upload_conn.close()

        _webdav_upload(config, merged_path)

        if has_remote:
            msg = f"同步成功 — 从远端拉取 {new_count} 条记录，上传 {uploaded_count} 条至云端"
        else:
            msg = f"首次同步成功，已上传 {uploaded_count} 条至云端"
        return {
            "status": "ok",
            "message": msg,
            "remote_records": new_count,
            "uploaded_records": uploaded_count,
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
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Config auto-sync ───────────────────────────────────────────────────────

CONFIG_TABLES = [
    "upstream_accounts",
    "local_keys",
    "model_pricing",
    "account_models",
    "key_model_map",
    "model_map_templates",
    "model_map_template_entries",
]


def sync_config_upload(db_path: str) -> bool:
    """Export all config tables to cloud (no request_log/perf_events)."""
    config = load_sync_config(db_path)
    if not config:
        return False

    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    config_path = str(tmp_dir / "proxy_config.db")

    try:
        _safe_copy_db(db_path, config_path)
        dst = sqlite3.connect(config_path)
        dst.execute("DELETE FROM request_log")
        dst.execute("DELETE FROM perf_events")
        dst.execute("DELETE FROM sync_config")
        dst.commit()
        dst.close()

        _webdav_upload(config, config_path, remote_filename="proxy_config.db")
        return True
    except (WebDAVError, Exception):
        import traceback
        traceback.print_exc()
        return False
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_download(db_path: str) -> bool:
    """Download config from cloud and merge into local DB (INSERT OR REPLACE)."""
    config = load_sync_config(db_path)
    if not config:
        return False

    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    remote_path = str(tmp_dir / "proxy_config_remote.db")

    try:
        has_remote = _webdav_download(config, remote_path, remote_filename="proxy_config.db")
        if not has_remote:
            return False

        remote_conn = sqlite3.connect(remote_path)
        local_conn = sqlite3.connect(db_path, timeout=10)
        local_conn.execute("PRAGMA busy_timeout=5000")

        for table in CONFIG_TABLES:
            try:
                rows = remote_conn.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    continue
                cols_desc = remote_conn.execute(f"SELECT * FROM {table} LIMIT 0").description
                columns = [d[0] for d in cols_desc]
                placeholders = ",".join(["?"] * len(columns))
                cols = ",".join(columns)
                for row in rows:
                    local_conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
                        row,
                    )
            except sqlite3.OperationalError:
                pass  # table missing in remote — skip

        local_conn.commit()
        local_conn.close()
        remote_conn.close()
        return True
    except (WebDAVError, Exception):
        import traceback
        traceback.print_exc()
        return False
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Dashboard DB sync ────────────────────────────────────────────────────

def sync_dashboard(proxy_db_path: str, dash_db_path: str) -> dict:
    """Sync the dashboard database via WebDAV.

    Args:
        proxy_db_path: Path to proxy.db (contains WebDAV config).
        dash_db_path: Path to dashboard.db (the data to sync).
    """
    project_root = Path(dash_db_path).resolve().parent
    tmp_dir = project_root / "tmp_dash"
    tmp_dir.mkdir(exist_ok=True)

    config = load_sync_config(proxy_db_path)
    if not config:
        return {"status": "error", "message": "未配置同步服务器"}

    db_path = dash_db_path  # use local variable for consistency

    # Same folder as proxy, different filename
    from dataclasses import replace
    dash_config = replace(config, filename="dashboard_sync.db")

    merged_path = str(tmp_dir / "dash_merged.db")

    try:
        # 1. Try download existing remote
        has_remote = _webdav_download(dash_config, merged_path)
        dash_count = 0

        if has_remote and os.path.exists(db_path):
            # Merge remote → local (INSERT OR REPLACE based on unique keys)
            local_conn = sqlite3.connect(db_path, timeout=10)
            local_conn.execute("PRAGMA busy_timeout=5000")
            remote_conn = sqlite3.connect(merged_path)
            remote_conn.execute("PRAGMA busy_timeout=5000")

            for table in ("token_usage", "request_usage", "cost_entry"):
                cols = _get_table_columns(remote_conn, table)
                if not cols:
                    continue
                col_list = ", ".join(cols)
                placeholders = ", ".join(["?"] * len(cols))
                rows = remote_conn.execute(f"SELECT {col_list} FROM {table}").fetchall()
                for row in rows:
                    try:
                        before = local_conn.total_changes
                        local_conn.execute(
                            f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
                            tuple(row),
                        )
                        if local_conn.total_changes > before:
                            dash_count += 1
                    except sqlite3.Error:
                        pass
            local_conn.commit()
            local_conn.close()
            remote_conn.close()

            # Layer 2: Delete any "unknown" entries merged from remote
            cleaned = _cleanup_unknown_entries(db_path)
            if cleaned > 0:
                print(f"[Sync] Cleaned up {cleaned} 'unknown' entries from remote merge", flush=True)

        # Layer 2: Clean up any local "unknown" entries before uploading
        if os.path.exists(db_path):
            cleaned = _cleanup_unknown_entries(db_path)
            if cleaned > 0:
                print(f"[Sync] Cleaned up {cleaned} local 'unknown' entries before upload", flush=True)

        # 2. Upload local copy (always, even if no remote)
        upload_count = 0
        if os.path.exists(db_path):
            _safe_copy_db(db_path, merged_path)
            uc = sqlite3.connect(merged_path)
            upload_count = uc.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
            upload_count += uc.execute("SELECT COUNT(*) FROM request_usage").fetchone()[0]
            upload_count += uc.execute("SELECT COUNT(*) FROM cost_entry").fetchone()[0]
            uc.close()
            _webdav_upload(dash_config, merged_path)

        if has_remote:
            msg = f"仪表板：从远端合并 {dash_count} 条，上传 {upload_count} 条"
        else:
            msg = f"仪表板：首次上传 {upload_count} 条至云端"
        return {"status": "ok", "message": msg, "dashboard_records": dash_count}

    except WebDAVError as e:
        return {"status": "error", "message": f"WebDAV 错误: {e}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": f"同步失败: {type(e).__name__}: {e}"}
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _cleanup_unknown_entries(db_path: str) -> int:
    """Layer 2: Delete all rows where model or api_key_name is 'unknown'.

    Returns total number of rows deleted.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    total = 0
    try:
        for table in ("token_usage", "request_usage", "cost_entry"):
            # Delete rows with model = 'unknown' (case-insensitive)
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE LOWER(model) = 'unknown'"
            )
            total += cursor.rowcount
            # For tables with api_key_name column, also delete those
            if table != "cost_entry":
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE LOWER(api_key_name) = 'unknown'"
                )
                total += cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return total


def _get_table_columns(conn, table: str) -> list[str]:
    """Get column names for a table, excluding 'id'."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows if r[1] != "id"]


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
        CREATE TABLE IF NOT EXISTS account_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            UNIQUE(account_id, model_id)
        );
        CREATE TABLE IF NOT EXISTS key_model_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            upstream_model TEXT NOT NULL,
            UNIQUE(key_id, pattern)
        );
        CREATE TABLE IF NOT EXISTS model_map_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS model_map_template_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            pattern TEXT NOT NULL,
            upstream_model TEXT NOT NULL
        );
    """)
    conn.commit()
