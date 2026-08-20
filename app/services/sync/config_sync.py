"""Functional WebDAV synchronization module."""

import logging

from app.services.sync.common import *  # noqa: F401,F403
from app.db.schema_upgrade import upgrade_downloaded_artifact
from app.db.schema_upgrade.coordinator import inspect_version
from app.db.migrations import schema_dir_for
from app.services.sync.settings import SyncConfig

log = logging.getLogger(__name__)


def _mark_sync_degraded(db_path: str, operation: str, exc: Exception) -> None:
    """Persist a sticky, user-visible sync failure without masking the cause."""
    message = f"{operation} degraded: {type(exc).__name__}: {exc}"
    try:
        _set_sync_state(db_path, "sync_health", message)
    except Exception:
        # The original exception is more useful than a secondary database
        # failure.  Keep the health write best-effort, but never silent.
        log.exception("failed to persist sync health for %s", operation)

def _sync_config_upload_once(db_path: str, schema_dir: str | None = None) -> dict:
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
        remote_artifact = _latest_artifact(config, "proxy_config")
        has_remote = bool(remote_artifact and _webdav_download(
            config, remote_path, remote_filename=remote_artifact.name))
        if not remote_artifact and _webdav_download(
                config, remote_path, remote_filename="proxy_config.db"):
            remote_artifact = RemoteArtifact("proxy_config.db")
            has_remote = True
        if has_remote:
            # Hash on the same schema basis as sync_config_download: the cloud
            # copy may predate the current migration (e.g. still carrying a
            # dropped column like cancellation_grace_hours), which would make
            # its row-hash differ from the stored one purely by schema, not by
            # content — causing a permanent conflict.  Upgrade it here so the
            # comparison is column-consistent.
            remote_version = inspect_version(Path(remote_path), "proxy")
            local_version = inspect_version(Path(db_path), "proxy")
            if (remote_version and local_version and
                    remote_version.major not in {0, local_version.major}):
                message = (f"拒绝跨 Major 配置同步: remote=V{remote_version.major}."
                           f"{remote_version.minor}, local=V{local_version.major}."
                           f"{local_version.minor}")
                _set_sync_state(db_path, "sync_health", message)
                return {"status": "error", "message": message, "conflict": False}
            upgrade_downloaded_artifact(
                remote_path, "proxy",
                schema_dir or schema_dir_for(db_path, "proxy"),
                local_proxy_path=db_path, configuration_only=True)
            upgraded_version = inspect_version(Path(remote_path), "proxy")
            if (upgraded_version and local_version and
                    upgraded_version.major == local_version.major and
                    upgraded_version.minor > local_version.minor):
                message = "云端 schema minor 高于本机，已进入只读兼容模式"
                _set_sync_state(db_path, "sync_health", message)
                return {"status": "error",
                        "message": message,
                        "conflict": False}
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
        dst = sqlite3.connect(config_path)
        try:
            for table in _RUNTIME_TABLES:
                if _table_exists(dst, table):
                    dst.execute(f"DELETE FROM {table}")
            _sanitize_upload_columns(dst)
            dst.commit()
        finally:
            dst.close()
        dst = sqlite3.connect(config_path)
        dst.execute("VACUUM")
        dst.close()

        # ── 3. Upload. ──
        published = _upload_versioned_artifact(
            config, config_path, "proxy_config", remote_artifact)
        _publish_schema_manifest(config, config_path, "proxy_config")

        # ── 4. Commit: record hash of what we uploaded + local snapshot. ──
        _set_sync_state(db_path, "config_hash", _config_hash_of_db(config_path))
        _set_sync_state(db_path, "remote_artifact", published.name)
        if published.etag:
            _set_sync_state(db_path, "remote_etag", published.etag)
        uploaded_version = inspect_version(Path(config_path), "proxy")
        record_remote_metadata(
            db_path, "proxy", _file_checksum(Path(config_path)),
            uploaded_version.major if uploaded_version else None,
            uploaded_version.minor if uploaded_version else None)
        snapshot_config(db_path)
        _set_sync_state(db_path, "sync_health", "ok")
        return {"status": "ok", "message": "配置已上传", "conflict": False}

    except WebDAVConflict:
        raise
    except WebDAVError as e:
        _mark_sync_degraded(db_path, "config upload", e)
        return {"status": "error", "message": f"WebDAV 错误: {e}", "conflict": False}
    except Exception as e:
        log.exception("config upload failed")
        _mark_sync_degraded(db_path, "config upload", e)
        return {"status": "error", "message": f"上传失败: {type(e).__name__}: {e}", "conflict": False}
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_upload(db_path: str, schema_dir: str | None = None) -> dict:
    """Upload configuration with bounded re-pull/rebuild retries.

    An immutable artifact can race with another node between the PROPFIND
    check and PUT.  Re-running the complete download/merge/build transaction
    is safe because the local snapshot/hash is only advanced after PUT.
    """
    last_error = None
    for attempt in range(3):
        try:
            return _sync_config_upload_once(db_path, schema_dir=schema_dir)
        except WebDAVConflict as exc:
            last_error = exc
            log.warning("config upload raced with remote update; retry %d/3",
                        attempt + 1)
    if last_error is not None:
        _mark_sync_degraded(db_path, "config upload conflict", last_error)
    return {"status": "conflict", "message": str(last_error), "conflict": True}


def _sync_config_download_once(db_path: str,
                               schema_dir: str | None = None) -> bool:
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
        remote_artifact = _latest_artifact(config, "proxy_config")
        has_remote = bool(remote_artifact and _webdav_download(
            config, remote_path, remote_filename=remote_artifact.name))
        if not remote_artifact and _webdav_download(
                config, remote_path, remote_filename="proxy_config.db"):
            remote_artifact = RemoteArtifact("proxy_config.db")
            has_remote = True
        if not has_remote:
            return False
        # Raw identity of the downloaded cloud file, captured before the
        # in-place upgrade below mutates the shadow.
        raw_sha256 = _file_checksum(Path(remote_path))
        # The cloud copy is a full-schema snapshot uploaded via _safe_copy_db
        # (backup API) and therefore carries user_version.  Upgrade it to the
        # local schema — exactly like the local DB — before merging, so a cloud
        # file from an older release (e.g. one still carrying a now-dropped
        # column like cancellation_grace_hours) cannot fight the local schema.
        remote_version = inspect_version(Path(remote_path), "proxy")
        local_version = inspect_version(Path(db_path), "proxy")
        if (remote_version and local_version and
                remote_version.major not in {0, local_version.major}):
            _set_sync_state(db_path, "sync_health", "remote major mismatch")
            return False
        upgrade_downloaded_artifact(
            remote_path, "proxy",
            schema_dir or schema_dir_for(db_path, "proxy"),
            local_proxy_path=db_path, configuration_only=True)
        upgraded_version = inspect_version(Path(remote_path), "proxy")
        if (upgraded_version and local_version and
                upgraded_version.major == local_version.major and
                upgraded_version.minor > local_version.minor):
            _set_sync_state(db_path, "sync_health", "remote minor is newer; write paused")
            return False
        _merge_config_tables(remote_path, db_path)
        if remote_version and remote_version.major == 0:
            publish_path = str(tmp_dir / "proxy_config_v1.db")
            _safe_copy_db(db_path, publish_path)
            dst = sqlite3.connect(publish_path)
            try:
                _sanitize_upload_columns(dst)
                dst.commit()
            finally:
                dst.close()
            published = _upload_versioned_artifact(
                config, publish_path, "proxy_config", remote_artifact)
            _publish_schema_manifest(config, publish_path, "proxy_config")
            _set_sync_state(db_path, "config_hash", _config_hash_of_db(publish_path))
            _set_sync_state(db_path, "remote_artifact", published.name)
            if published.etag:
                _set_sync_state(db_path, "remote_etag", published.etag)
            published_version = inspect_version(Path(publish_path), "proxy")
            record_remote_metadata(
                db_path, "proxy", _file_checksum(Path(publish_path)),
                published_version.major if published_version else None,
                published_version.minor if published_version else None)
        else:
            _set_sync_state(db_path, "config_hash", _config_hash_of_db(remote_path))
            if remote_artifact:
                _set_sync_state(db_path, "remote_artifact", remote_artifact.name)
                if remote_artifact.etag:
                    _set_sync_state(db_path, "remote_etag", remote_artifact.etag)
            record_remote_metadata(
                db_path, "proxy", raw_sha256,
                remote_version.major if remote_version else None,
                remote_version.minor if remote_version else None)
        snapshot_config(db_path)
        _set_sync_state(db_path, "sync_health", "ok")
        return True
    except WebDAVConflict:
        raise
    except Exception as exc:
        log.exception("config download failed")
        _mark_sync_degraded(db_path, "config download", exc)
        return False
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_download(db_path: str, schema_dir: str | None = None) -> bool:
    """Download configuration, retrying a concurrent immutable-artifact race."""
    for attempt in range(3):
        try:
            return _sync_config_download_once(db_path, schema_dir=schema_dir)
        except WebDAVConflict:
            log.warning("config download raced with remote update; retry %d/3",
                        attempt + 1)
    _mark_sync_degraded(
        db_path, "config download conflict",
        WebDAVConflict("remote artifact changed during download"))
    return False
