"""WebDAV-based database sync for multi-machine proxy usage.

Syncs:
  - Config tables (upstream_accounts, local_keys, model_pricing, etc.)
  - Dashboard archive (token_usage, request_usage, cost_entry, proxy_plan_summary)

request_log is NOT synced — it is local-only. Dashboard sync is a
pull-export-upload transaction: **cloud is always the latest; every machine's
local dashboard.db is always a historical version of the cloud**. Progress is
tracked by a single high-water mark (sync_state.last_exported_log_id) that is
advanced only AFTER a successful upload — any failed step rolls back by
discarding the shadow db (no partial state, no per-row markers).
"""

import hashlib
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
    "pricing_slots",
    "account_models",
    "aggregate_entries",
    "proxy_timeout_config",
    "plan_billing_config",
    "plan_price_history",
]

# Runtime/secret tables stripped from the upload copy before it ever reaches
# the cloud. sync_state holds the machine-local sync watermark + config hash.
# upstream_keys (the per-account multi-key set) and session_key_log (session→
# key observability) are local secrets — never uploaded.
_RUNTIME_TABLES = [
    "request_log",
    "perf_events",
    "sync_config",
    "in_flight_requests",
    "sync_state",
    "upstream_keys",
    "session_key_log",
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _merge_upstream_keys_cloud(remote_path: str, local_path: str) -> None:
    """Merge masked key metadata as an additive, idempotent union.

    The table deliberately stays outside the normal config hash: two machines
    can have different local secrets.  Therefore every upload and download
    explicitly unions it instead of allowing a stale upload to erase rows.
    A deletion is a tombstone: once observed it wins over an active copy.
    """
    remote = sqlite3.connect(remote_path)
    remote.row_factory = sqlite3.Row
    local = sqlite3.connect(local_path)
    local.row_factory = sqlite3.Row
    try:
        if not _table_exists(remote, "upstream_keys_cloud") or not _table_exists(local, "upstream_keys_cloud"):
            return
        local.execute("BEGIN IMMEDIATE")
        for incoming in remote.execute("SELECT * FROM upstream_keys_cloud"):
            current = local.execute(
                "SELECT * FROM upstream_keys_cloud WHERE account_id=? AND key_masked=?",
                (incoming["account_id"], incoming["key_masked"]),
            ).fetchone()
            if current is None:
                cols = [d[0] for d in remote.execute("SELECT * FROM upstream_keys_cloud LIMIT 0").description]
                local.execute(
                    f"INSERT INTO upstream_keys_cloud ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    [incoming[c] for c in cols],
                )
                continue
            valid_values = [v for v in (current["valid_from"], incoming["valid_from"]) if v]
            deleted_values = [v for v in (current["deleted_at"], incoming["deleted_at"]) if v]
            # Keep the grace value attached to the earliest cancellation.
            deleted_at = min(deleted_values) if deleted_values else None
            grace = current["cancellation_grace_hours"]
            if incoming["deleted_at"] and (
                not current["deleted_at"] or incoming["deleted_at"] <= current["deleted_at"]
            ):
                grace = incoming["cancellation_grace_hours"]
            local.execute(
                "UPDATE upstream_keys_cloud SET position=?, valid_from=?, deleted_at=?, "
                "cancellation_grace_hours=? WHERE account_id=? AND key_masked=?",
                (min(current["position"], incoming["position"]),
                 min(valid_values) if valid_values else None, deleted_at, grace,
                 incoming["account_id"], incoming["key_masked"]),
            )
        local.commit()
    except Exception:
        local.rollback()
        raise
    finally:
        remote.close()
        local.close()


def _config_hash_of_db(db_path: str) -> str:
    """sha256 over the cloud-representation (upstream keys stripped) of
    CONFIG_TABLES in *db_path*. Used for cross-machine conflict detection:
    the hash is invariant to each machine's locally-entered upstream keys."""
    conn = sqlite3.connect(db_path)
    h = hashlib.sha256()
    try:
        for table in CONFIG_TABLES:
            if not _table_exists(conn, table):
                continue
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
            if table == "upstream_accounts":
                cols = [c for c in cols if c != "upstream_key"]
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


def _snapshot_path(db_path: str) -> str:
    """Local-only snapshot of the last-committed config (includes this
    machine's upstream keys — never uploaded). Used to roll back a failed
    upload transaction ("discard")."""
    return str(Path(db_path).resolve().parent / "config_snapshot.db")


def snapshot_config(db_path: str) -> None:
    """Copy CONFIG_TABLES (+ the local-only upstream_keys, WITH their values —
    keys are secrets that must survive a "discard" rollback) into the snapshot."""
    snap = _snapshot_path(db_path)
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(snap)
    try:
        dst.execute("PRAGMA foreign_keys=OFF")
        for table in CONFIG_TABLES + ["upstream_keys", "upstream_keys_cloud"]:
            if not _table_exists(src, table):
                continue
            cols = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]
            dst.execute(f"DROP TABLE IF EXISTS {table}")
            dst.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                ph = ",".join("?" for _ in cols)
                dst.executemany(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
        dst.commit()
    finally:
        src.close()
        dst.close()


def restore_config_snapshot(db_path: str) -> bool:
    """Roll CONFIG_TABLES back to the last-committed snapshot. Returns False
    if no snapshot exists. Single transaction.

    Uses foreign_keys=OFF while swapping config tables so deleting/re-inserting
    upstream_accounts does NOT detach request_log (ON DELETE SET NULL would
    null every historical account_id). Deletes children before parents and
    inserts parents before children, so the config tables stay FK-consistent.
    After the swap, request_log rows pointing at an account that was discarded
    (created after the snapshot, then rolled back) are detached — account_id
    becomes NULL, which the dashboard renders as "unknown".
    """
    snap_path = _snapshot_path(db_path)
    if not os.path.exists(snap_path):
        return False
    snap = sqlite3.connect(snap_path)
    snap.row_factory = sqlite3.Row
    local = sqlite3.connect(db_path, timeout=10)
    try:
        local.execute("PRAGMA busy_timeout=5000")
        local.execute("PRAGMA foreign_keys=OFF")
        local.execute("BEGIN IMMEDIATE")
        children = ["aggregate_entries", "account_models", "local_keys",
                    "pricing_slots", "proxy_timeout_config", "plan_price_history",
                    "upstream_keys", "upstream_keys_cloud"]
        parents = ["upstream_accounts", "model_pricing"]
        # Delete child-first, then parents.
        for table in children + parents:
            if _table_exists(local, table) and _table_exists(snap, table):
                local.execute(f"DELETE FROM {table}")
        # Insert parents first, then children.
        for table in parents + children:
            if not _table_exists(snap, table):
                continue
            cols = [d[0] for d in snap.execute(f"SELECT * FROM {table} LIMIT 0").description]
            rows = snap.execute(f"SELECT * FROM {table}").fetchall()
            ph = ",".join("?" for _ in cols)
            for r in rows:
                local.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
                    [r[c] for c in cols],
                )
        # Detach logs whose account was discarded by the rollback (account_id
        # → NULL; the dashboard renders these as "unknown").
        if _table_exists(local, "request_log"):
            local.execute(
                "UPDATE request_log SET account_id = NULL "
                "WHERE account_id IS NOT NULL AND NOT EXISTS ("
                "  SELECT 1 FROM upstream_accounts "
                "  WHERE upstream_accounts.id = request_log.account_id)"
            )
        local.commit()
        return True
    finally:
        snap.close()
        local.close()


def _merge_config_tables(remote_path: str, local_path: str) -> None:
    """Cloud-authoritative merge of CONFIG_TABLES from *remote_path* into
    *local_path* in one transaction.

    - upstream_accounts: upsert by id; preserve this machine's non-empty
      upstream_key (the cloud never carries keys); delete local rows absent
      from the cloud (delete-stale).
    - local_keys: upsert by key_value (globally unique), delete-stale.
    - model_pricing: upsert by id, delete-stale.
    - account_models / aggregate_entries / pricing_slots /
      proxy_timeout_config: replaced wholesale from the cloud.
    """
    _merge_upstream_keys_cloud(remote_path, local_path)
    remote = sqlite3.connect(remote_path)
    remote.row_factory = sqlite3.Row
    local = sqlite3.connect(local_path, timeout=10)
    local.row_factory = sqlite3.Row
    try:
        local.execute("PRAGMA busy_timeout=5000")
        local.execute("PRAGMA foreign_keys=ON")
        local.execute("BEGIN IMMEDIATE")

        def cols(conn, table):
            return [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]

        def safe_insert(table, col_list, vals):
            ph = ",".join("?" for _ in col_list)
            try:
                local.execute(
                    f"INSERT INTO {table} ({','.join(col_list)}) VALUES ({ph})", vals)
            except sqlite3.IntegrityError:
                pass  # orphan reference in a stale cloud file — skip

        # ── Phase A: clear replace-wholesale child tables first, so parent
        #    delete-stale never trips a FOREIGN KEY (account_models has no
        #    ON DELETE clause). ──
        for table in ("account_models", "aggregate_entries", "pricing_slots",
                      "proxy_timeout_config", "plan_billing_config", "plan_price_history"):
            # A cloud config created by an older release does not have newly
            # introduced tables.  Preserve local defaults/history in that
            # case instead of clearing them before there is remote data to
            # restore.
            if _table_exists(local, table) and _table_exists(remote, table):
                local.execute(f"DELETE FROM {table}")

        # ── upstream_accounts: upsert by id, preserve local key, delete-stale ──
        if _table_exists(remote, "upstream_accounts"):
            r_accounts = {r["id"]: r for r in remote.execute("SELECT * FROM upstream_accounts")}
            l_accounts = {r["id"]: r for r in local.execute("SELECT * FROM upstream_accounts")}
            acc_cols = cols(remote, "upstream_accounts")
            nonkey = [c for c in acc_cols if c not in ("id", "upstream_key")]
            for aid, r in r_accounts.items():
                if aid in l_accounts:
                    sql = ("UPDATE upstream_accounts SET " +
                           ", ".join(f"{c}=?" for c in nonkey) +
                           ", upstream_key=? WHERE id=?")
                    local.execute(sql, [r[c] for c in nonkey] +
                                  [l_accounts[aid]["upstream_key"] or ""] + [aid])
                else:
                    vals = [r[c] if c != "upstream_key" else "" for c in acc_cols]
                    safe_insert("upstream_accounts", acc_cols, vals)
            for aid in l_accounts:
                if aid not in r_accounts:
                    local.execute("DELETE FROM upstream_accounts WHERE id=?", (aid,))

        # ── local_keys: upsert by key_value, delete-stale ──
        if _table_exists(remote, "local_keys"):
            r_keys = {r["key_value"]: r for r in remote.execute("SELECT * FROM local_keys")}
            l_keys = {r["key_value"]: r for r in local.execute("SELECT * FROM local_keys")}
            key_cols = cols(remote, "local_keys")
            for kv, r in r_keys.items():
                if kv in l_keys:
                    local.execute(
                        "UPDATE local_keys SET label=?, account_id=? WHERE key_value=?",
                        (r["label"], r["account_id"], kv))
                else:
                    safe_insert("local_keys", key_cols, [r[c] for c in key_cols])
            for kv in l_keys:
                if kv not in r_keys:
                    local.execute("DELETE FROM local_keys WHERE key_value=?", (kv,))

        # ── model_pricing: upsert by id, delete-stale ──
        if _table_exists(remote, "model_pricing"):
            r_p = {r["id"]: r for r in remote.execute("SELECT * FROM model_pricing")}
            l_p = {r["id"]: r for r in local.execute("SELECT * FROM model_pricing")}
            p_cols = cols(remote, "model_pricing")
            fields = [c for c in p_cols if c != "id"]
            for pid, r in r_p.items():
                if pid in l_p:
                    local.execute(
                        f"UPDATE model_pricing SET {', '.join(f'{c}=?' for c in fields)} WHERE id=?",
                        [r[c] for c in fields] + [pid])
                else:
                    safe_insert("model_pricing", p_cols, [r[c] for c in p_cols])
            for pid in l_p:
                if pid not in r_p:
                    local.execute("DELETE FROM model_pricing WHERE id=?", (pid,))

        # ── Phase B: rebuild the replace-wholesale tables from the cloud
        #    (ids preserved, so no remapping is needed). ──
        for table in ("account_models", "aggregate_entries", "pricing_slots",
                      "proxy_timeout_config", "plan_billing_config", "plan_price_history"):
            if _table_exists(remote, table):
                c = cols(remote, table)
                for r in remote.execute(f"SELECT * FROM {table}"):
                    safe_insert(table, c, [r[col] for col in c])

        local.commit()
    finally:
        remote.close()
        local.close()


def sync_config_upload(db_path: str) -> dict:
    """Upload local config to cloud as one conflict-checked transaction.

    Returns {status: 'ok'|'conflict'|'error', message, conflict}.
    The uploaded file never carries upstream keys (stripped) or the WebDAV
    credentials / runtime tables. On success the local snapshot and the
    config hash are updated (commit point).
    """
    config = load_sync_config(db_path)
    if not config:
        return {"status": "unconfigured", "message": "未配置同步服务器", "conflict": False}

    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    config_path = str(tmp_dir / "proxy_config.db")
    remote_path = str(tmp_dir / "proxy_config_remote.db")

    try:
        # ── 1. Conflict check: refuse if the cloud moved past our last sync. ──
        has_remote = _download_latest(config, remote_path, base="proxy_config")
        if has_remote:
            last_hash = _get_sync_state(db_path, "config_hash")
            cloud_hash = _config_hash_of_db(remote_path)
            if last_hash is None or cloud_hash != last_hash:
                return {
                    "status": "conflict",
                    "message": "云端配置已被其他机器修改(或本机尚未下载过),已拒绝覆盖。"
                               "请重启仪表板拉取云端配置合并后再上传。",
                    "conflict": True,
                }

        # ── 2. Build upload copy: strip secrets, drop runtime tables. ──
        _safe_copy_db(db_path, config_path)
        if has_remote:
            # config_hash intentionally excludes per-machine masked metadata;
            # merge it explicitly before this upload copy becomes authoritative.
            _merge_upstream_keys_cloud(remote_path, config_path)
        dst = sqlite3.connect(config_path)
        try:
            for table in _RUNTIME_TABLES:
                if _table_exists(dst, table):
                    dst.execute(f"DELETE FROM {table}")
            dst.execute("UPDATE upstream_accounts SET upstream_key = ''")
            dst.commit()
        finally:
            dst.close()
        dst = sqlite3.connect(config_path)
        dst.execute("VACUUM")
        dst.close()

        # ── 3. Upload. ──
        _webdav_upload(config, config_path,
                       remote_filename=_make_timestamped_name("proxy_config.db"))

        # ── 4. Commit: record hash of what we uploaded + local snapshot. ──
        _set_sync_state(db_path, "config_hash", _config_hash_of_db(config_path))
        snapshot_config(db_path)
        return {"status": "ok", "message": "配置已上传", "conflict": False}

    except WebDAVError as e:
        return {"status": "error", "message": f"WebDAV 错误: {e}", "conflict": False}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": f"上传失败: {type(e).__name__}: {e}", "conflict": False}
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_download(db_path: str) -> bool:
    """Pull the latest cloud config and merge cloud-authoritatively into the
    local DB. On success the snapshot + config hash are updated (commit point)."""
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
        _merge_config_tables(remote_path, db_path)
        _set_sync_state(db_path, "config_hash", _config_hash_of_db(remote_path))
        snapshot_config(db_path)
        return True
    except (WebDAVError, Exception):
        import traceback
        traceback.print_exc()
        return False
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Dashboard archive sync (cloud-authoritative transaction) ──────────────

def sync_dashboard(proxy_db_path: str, dash_db_path: str) -> dict:
    """Sync the dashboard archive via WebDAV — one atomic transaction.

    Flow: pull (cloud → shadow) → export (request_log → shadow) →
    upload (shadow → cloud) → commit (advance mark, replace local, cleanup).

    Cloud is always the latest; local is a historical version of the cloud.
    If any step fails the shadow is discarded and nothing changes — a failed
    upload never advances the high-water mark, so nothing is ever lost.

    Args:
        proxy_db_path: Path to proxy.db (WebDAV config + request_log).
        dash_db_path: Path to dashboard.db (the local archive to replace).
    """
    project_root = Path(dash_db_path).resolve().parent
    tmp_dir = project_root / "tmp_dash"
    tmp_dir.mkdir(exist_ok=True)

    config = load_sync_config(proxy_db_path)
    if not config:
        return {"status": "error", "message": "未配置同步服务器"}

    try:
        # 1. Pull: download latest cloud archive into the shadow. No cloud file
        #    yet (first sync) → seed the shadow from the current local archive
        #    so the historical baseline is preserved.
        shadow_path = str(tmp_dir / "dash_shadow.db")
        has_remote = _download_latest(config, shadow_path, base="dashboard_sync")
        if not has_remote and os.path.exists(dash_db_path):
            _safe_copy_db(dash_db_path, shadow_path)

        # 2. Bring the shadow up to the current schema (cloud may be older).
        from app.migrations import migrate, schema_dir_for
        migrate(shadow_path, schema_dir_for(dash_db_path, "dashboard"))

        # 2b. Reconcile: mirror upstream_accounts (id → name/type/deleted_at)
        #     into the shadow's `accounts` table and backfill any legacy
        #     name-keyed buckets to their account_id. Runs before export so the
        #     fresh rows and the archive both key on account_id.
        from app.dashboard_db import reconcile_accounts
        reconcile_accounts(shadow_path, proxy_db_path)

        # 3. Export: request_log rows in (mark, max_id] → shadow, additively.
        from app.proxy_db import ProxyDatabase
        proxy_db = ProxyDatabase(proxy_db_path)
        mark = proxy_db.get_export_mark()
        max_id = proxy_db.get_max_log_id()
        export_result = proxy_db.export_to_dashboard(shadow_path, mark, max_id)

        # 4. Upload the shadow → cloud (cloud is always the latest).
        _webdav_upload(config, shadow_path,
                       remote_filename=_make_timestamped_name("dashboard_sync.db"))

        # 5. COMMIT — upload succeeded:
        #    a. advance the high-water mark (these rows are confirmed on cloud);
        #    b. replace the local archive with the shadow;
        #    c. clean up archived rows older than 30 days.
        proxy_db.set_export_mark(max_id)
        _safe_copy_db(shadow_path, dash_db_path)
        cleaned = proxy_db.cleanup_exported_logs(max_id)
        if cleaned > 0:
            print(f"[Sync] Cleaned up {cleaned} archived request_log rows", flush=True)

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
