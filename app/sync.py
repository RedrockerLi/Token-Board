"""WebDAV-based database sync for multi-machine proxy usage.

Syncs:
  - Config tables (upstream_accounts, local_keys, model_pricing, etc.)
  - Dashboard database (token_usage, request_usage, model_pricing, cost_entry)

request_log is NOT synced — it is local-only, with an exported flag to track
which rows have been exported to the dashboard database.
"""

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
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

def save_sync_config(db_path: str, config: SyncConfig):
    conn = sqlite3.connect(db_path)
    try:
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


# ── Timestamped file helpers ─────────────────────────────────────────────

def _make_timestamped_name(base: str) -> str:
    """dashboard_sync.db → dashboard_sync_20260716_143025.db"""
    name, ext = base.rsplit(".", 1)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{name}_{stamp}.{ext}"


def _list_folder_files(config, prefix: str) -> list[str]:
    """List filenames in the sync folder matching a prefix via PROPFIND.

    Returns sorted list of matching filenames.
    """
    folder_url = f"{config.base_url.rstrip('/')}/{config.folder.strip('/')}/"
    resp = requests.request(
        "PROPFIND", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=15,
        headers={"Depth": "1"},
    )
    if not resp.ok:
        return []

    # Parse XML to extract hrefs. Minimal parser — just regex the filenames.
    # WebDAV PROPFIND returns multistatus XML with <D:href> elements.
    names = []
    for href in re.findall(r"<[Dd]:href>([^<]+)</[Dd]:href>", resp.text):
        fn = href.rstrip("/").rsplit("/", 1)[-1]
        if fn and fn.startswith(prefix):
            names.append(fn)
    return sorted(names)


def _download_latest(config, dest_path: str, base: str) -> bool:
    """Find the latest timestamped file matching *base* and download it.

    base = "dashboard_sync" → matches "dashboard_sync_20260716_143025.db"
    Returns True if a file was found and downloaded, False if no files exist.
    """
    prefix = base + "_"
    files = _list_folder_files(config, prefix)
    if not files:
        # Also try the bare name for backward compatibility
        return _webdav_download(config, dest_path, remote_filename=base + ".db")

    latest = files[-1]  # sorted alphabetically = chronologically
    return _webdav_download(config, dest_path, remote_filename=latest)


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


# ── DB helpers ────────────────────────────────────────────────────────────

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


def _count_dashboard_rows(db_path: str) -> int:
    """Count total rows across all dashboard tables."""
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0]
        total += conn.execute("SELECT COUNT(*) FROM request_usage").fetchone()[0]
        total += conn.execute("SELECT COUNT(*) FROM cost_entry").fetchone()[0]
        return total
    finally:
        conn.close()


# ── Config auto-sync ───────────────────────────────────────────────────────

CONFIG_TABLES = [
    "upstream_accounts",
    "local_keys",
    "model_pricing",
    "account_models",
    "aggregate_entries",
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
        dst.execute("DELETE FROM in_flight_requests")
        dst.commit()
        dst.execute("VACUUM")
        dst.close()

        _webdav_upload(config, config_path, remote_filename=_make_timestamped_name("proxy_config.db"))
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
        has_remote = _download_latest(config, remote_path, base="proxy_config")
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
                    # INSERT OR IGNORE: local config wins. Cloud is transport.
                    local_conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})",
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


# ── Dashboard merge helper ──────────────────────────────────────────────

def _merge_dashboard(remote_path: str, local_path: str):
    """Copy remote dashboard rows into local via INSERT OR REPLACE.

    Remote data overwrites local — the cloud copy is the authoritative
    merged state from all machines. Any local data not yet in the remote
    (because a previous upload failed) will be restored by the subsequent
    export step, since those request_log rows still have exported=0.
    """

    TABLES = {
        "token_usage": ["date", "model", "api_key_name", "token_type", "amount", "cost_group_key"],
        "request_usage": ["date", "model", "api_key_name", "count"],
        "cost_entry": ["date", "model", "cost", "cost_group_key", "source"],
        "model_pricing": ["id", "model_pattern", "input_price", "output_price",
                          "cache_read_price", "currency"],
        "account_types": ["account_name", "account_type"],
        "proxy_plan_summary": ["month", "account_name", "subscription_cost", "virtual_cost"],
    }

    remote_conn = sqlite3.connect(remote_path)
    local_conn = sqlite3.connect(local_path, timeout=10)
    local_conn.execute("PRAGMA busy_timeout=5000")
    try:
        for table, cols in TABLES.items():
            try:
                cols_s = ", ".join(cols)
                phs = ", ".join(["?"] * len(cols))
                rows = remote_conn.execute(f"SELECT {cols_s} FROM {table}").fetchall()
                for row in rows:
                    local_conn.execute(
                        f"INSERT OR REPLACE INTO {table} ({cols_s}) VALUES ({phs})", row
                    )
            except sqlite3.OperationalError:
                pass
        local_conn.commit()
    finally:
        local_conn.close()
        remote_conn.close()


# ── Dashboard DB sync ────────────────────────────────────────────────────

def sync_dashboard(proxy_db_path: str, dash_db_path: str) -> dict:
    """Sync the dashboard database via WebDAV.

    Flow: download remote → export new local data → upload merged result.
    Multi-machine safe when machines are not concurrent (pull-modify-push).

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

    try:
        # 1. Download latest remote dashboard.db to temp (pick most recent by timestamp)
        remote_path = str(tmp_dir / "dash_remote.db")
        has_remote = _download_latest(config, remote_path, base="dashboard_sync")

        # 2. Merge remote → local
        if has_remote:
            _merge_dashboard(remote_path, dash_db_path)
            print(f"[Sync] Dashboard: merged latest from cloud", flush=True)

        # 3. Export unexported request_log → dashboard (syncs model_pricing too)
        from app.proxy_db import ProxyDatabase
        proxy_db = ProxyDatabase(proxy_db_path)
        export_result = proxy_db.export_to_dashboard(mark_exported=False)

        # 4. Cleanup old exported request_log rows
        cleaned = proxy_db.cleanup_exported_logs(max_exported=10000)
        if cleaned > 0:
            print(f"[Sync] Cleaned up {cleaned} old exported request_log rows", flush=True)

        # 5. Upload merged dashboard.db to cloud with timestamp
        upload_path = str(tmp_dir / "dash_upload.db")
        _safe_copy_db(dash_db_path, upload_path)
        _webdav_upload(config, upload_path,
                       remote_filename=_make_timestamped_name("dashboard_sync.db"))

        # 6. Upload succeeded — confirm: exported 1→2
        proxy_db.mark_uploaded()

        upload_count = _count_dashboard_rows(dash_db_path)
        msg = (
            f"仪表板：导出 {export_result.get('record_count', 0)} 条，"
            f"上传 {upload_count} 条至云端"
        )
        return {"status": "ok", "message": msg, "dashboard_records": upload_count}

    except WebDAVError as e:
        return {"status": "error", "message": f"WebDAV 错误: {e}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": f"同步失败: {type(e).__name__}: {e}"}
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
