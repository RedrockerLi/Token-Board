"""Functional WebDAV synchronization module."""

import logging

from app.services.sync.common import *  # noqa: F401,F403
from app.db.schema_upgrade import upgrade_downloaded_artifact
from app.db.schema_upgrade.coordinator import inspect_version
from app.db.migrations import schema_dir_for

log = logging.getLogger(__name__)
from app.services.sync.settings import SyncConfig


def _mark_sync_degraded(db_path: str, operation: str, exc: Exception) -> None:
    """Persist a sticky dashboard-sync failure for health/reporting APIs."""
    message = f"{operation} degraded: {type(exc).__name__}: {exc}"
    try:
        _set_sync_state(db_path, "sync_health", message)
    except Exception:
        log.exception("failed to persist sync health for %s", operation)

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
    """Count rows in the normalized V1 dashboard archive."""
    conn = sqlite3.connect(db_path)
    try:
        return (conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0] +
                conn.execute("SELECT COUNT(*) FROM monthly_recurring_costs").fetchone()[0])
    finally:
        conn.close()


def _sync_dashboard_once(proxy_db_path: str, dash_db_path: str,
                         schema_dir: str | None = None) -> dict:
    """Sync the dashboard archive via WebDAV — one atomic transaction.

    Flow: pull (cloud → shadow) → export (request_log → shadow) →
    upload (shadow → cloud) → commit (advance mark, replace local, cleanup).

    Cloud is always the latest; local is a historical version of the cloud.
    If any step fails the shadow is discarded and nothing changes — a failed
    upload never advances the high-water mark, so nothing is ever lost.

    Args:
        proxy_db_path: Path to token-board.db (WebDAV config + request_log).
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
        remote_artifact = _latest_artifact(config, "dashboard_sync")
        has_remote = bool(remote_artifact and _webdav_download(
            config, shadow_path, remote_filename=remote_artifact.name))
        if not remote_artifact and _webdav_download(
                config, shadow_path, remote_filename="dashboard_sync.db"):
            remote_artifact = RemoteArtifact("dashboard_sync.db")
            has_remote = True
        if not has_remote and os.path.exists(dash_db_path):
            _safe_copy_db(dash_db_path, shadow_path)

        # 2. Bring the shadow up to the current schema (cloud may be older).
        raw_sha256 = _file_checksum(Path(shadow_path))
        remote_version = inspect_version(Path(shadow_path), "dashboard")
        local_version = inspect_version(Path(dash_db_path), "dashboard")
        if (remote_version and local_version and
                remote_version.major not in {0, local_version.major}):
            _set_sync_state(proxy_db_path, "sync_health", "remote major mismatch")
            return {"status": "error", "message": "云端 dashboard 跨 Major，已暂停同步"}
        resolved_schema_dir = schema_dir or schema_dir_for(
            dash_db_path, "dashboard")
        upgrade_downloaded_artifact(
            shadow_path, "dashboard", resolved_schema_dir,
            local_proxy_path=proxy_db_path)
        upgraded_version = inspect_version(Path(shadow_path), "dashboard")
        if (upgraded_version and local_version and
                upgraded_version.major == local_version.major and
                upgraded_version.minor > local_version.minor):
            _set_sync_state(proxy_db_path, "sync_health",
                            "remote minor is newer; write paused")
            return {"status": "error", "message": "云端 dashboard minor 更高，已暂停写入"}

        # 2b. Reconcile the normalized V1 account mirror before exporting.
        from app.db.dashboard_db import reconcile_accounts
        reconcile_accounts(shadow_path, proxy_db_path)

        # 3. Export: request_log rows in (mark, max_id] → shadow, additively.
        from app.db.proxy_db import ProxyDatabase
        proxy_db = ProxyDatabase(proxy_db_path, schema_dir=resolved_schema_dir)
        mark = proxy_db.get_export_mark()
        max_id = proxy_db.get_max_log_id()
        export_result = proxy_db.export_to_dashboard(shadow_path, mark, max_id)

        # 3b. Purge zero-usage (failed/test) buckets from the shadow before it
        #     becomes the authoritative archive, so neither the cloud copy nor
        #     the local dashboard.db carries all-zero model cards. export only
        #     adds rows; without this, failed-request rows already in the cloud
        #     archive would survive forever.
        from app.db.dashboard_db import DashboardDatabase
        purge_db = DashboardDatabase(
            shadow_path, schema_dir=resolved_schema_dir)
        purged = purge_db.purge_zero_usage_rows()
        if purged > 0:
            log.info("purged zero-usage archive rows: count=%d", purged)

        # 4. Upload the shadow → cloud (cloud is always the latest).
        published = _upload_versioned_artifact(
            config, shadow_path, "dashboard_sync", remote_artifact)
        _publish_schema_manifest(config, shadow_path, "dashboard_sync")

        # 5. COMMIT — upload succeeded:
        #    a. advance the high-water mark (these rows are confirmed on cloud);
        #    b. replace the local archive with the shadow;
        #    c. clean up archived rows older than 30 days.
        proxy_db.set_export_mark(max_id)
        _set_sync_state(proxy_db_path, "dashboard_remote_artifact", published.name)
        if published.etag:
            _set_sync_state(proxy_db_path, "dashboard_remote_etag", published.etag)
        record_remote_metadata(
            proxy_db_path, "dashboard", raw_sha256,
            remote_version.major if remote_version else None,
            remote_version.minor if remote_version else None)
        _safe_copy_db(shadow_path, dash_db_path)
        cleaned = proxy_db.cleanup_exported_logs(max_id)
        if cleaned > 0:
            log.info("cleaned archived request_log rows: count=%d", cleaned)

        upload_count = _count_dashboard_rows(dash_db_path)
        msg = (
            f"仪表板：导出 {export_result.get('record_count', 0)} 条，"
            f"上传 {upload_count} 条至云端"
        )
        _set_sync_state(proxy_db_path, "sync_health", "ok")
        return {"status": "ok", "message": msg, "dashboard_records": upload_count}

    except WebDAVConflict:
        raise
    except WebDAVError as e:
        _mark_sync_degraded(proxy_db_path, "dashboard sync", e)
        return {"status": "error", "message": f"WebDAV 错误: {e}"}
    except Exception as e:
        log.exception("dashboard sync failed")
        _mark_sync_degraded(proxy_db_path, "dashboard sync", e)
        return {"status": "error", "message": f"同步失败: {type(e).__name__}: {e}"}
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_dashboard(proxy_db_path: str, dash_db_path: str,
                   schema_dir: str | None = None) -> dict:
    """Run the cloud archive transaction with bounded conflict retries."""
    last_error = None
    for attempt in range(3):
        try:
            return _sync_dashboard_once(
                proxy_db_path, dash_db_path, schema_dir=schema_dir)
        except WebDAVConflict as exc:
            last_error = exc
            log.warning("dashboard upload raced with remote update; retry %d/3",
                        attempt + 1)
    if last_error is not None:
        _mark_sync_degraded(proxy_db_path, "dashboard sync conflict", last_error)
    return {"status": "conflict", "message": str(last_error)}
