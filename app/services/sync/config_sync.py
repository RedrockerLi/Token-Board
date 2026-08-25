"""Configuration synchronization workflow."""

import logging
import os
import shutil
from pathlib import Path

from app.core import sqlite_runtime
from app.db.schema_upgrade import upgrade_downloaded_artifact
from app.db.schema_upgrade.coordinator import inspect_version
from app.db.migrations import TOKEN_BOARD_DATABASE_NAME, schema_dir_for
from app.services.sync.common import RUNTIME_TABLE_DENYLIST
from app.services.sync.config_merge import merge_config_tables, sanitize_upload_columns
from app.services.sync.settings import SyncConfig, load_sync_config
from app.services.sync.snapshot import snapshot_config
from app.services.sync.state import (
    clear_sync_state_many,
    config_hash_of_db,
    get_sync_state,
    record_remote_metadata,
    set_sync_state,
    set_sync_state_many,
    table_exists,
)
from app.services.sync.storage import safe_copy_db
from app.services.sync.webdav import (
    WebDAVConflict,
    WebDAVError,
    download_artifact,
    find_artifact,
    file_checksum,
    latest_artifact,
    publish_schema_manifest,
    publish_versioned_artifact,
)

log = logging.getLogger(__name__)


def _mark_sync_degraded(db_path: str, operation: str, exc: Exception) -> None:
    """Persist a sticky, user-visible sync failure without masking the cause."""
    message = f"{operation} degraded: {type(exc).__name__}: {exc}"
    try:
        set_sync_state(db_path, "sync_health", message)
    except Exception:
        # The original exception is more useful than a secondary database
        # failure.  Keep the health write best-effort, but never silent.
        log.exception("failed to persist sync health for %s", operation)


_CONFIG_PENDING_KEYS = (
    "config_pending_path",
    "config_pending_remote_artifact",
    "config_pending_remote_etag",
)


def _pending_config_path(db_path: str) -> str:
    path = Path(db_path)
    return str(path.with_name(f".{path.stem}.pending{path.suffix}"))


def _prepare_config_pending(db_path: str, source_path: str) -> str:
    pending_path = _pending_config_path(db_path)
    safe_copy_db(source_path, pending_path)
    set_sync_state_many(db_path, {
        "config_pending_path": pending_path,
        "config_pending_remote_artifact": "",
        "config_pending_remote_etag": "",
    })
    return pending_path


def _commit_config_pending(db_path: str, pending_path: str,
                           published, *, recovered: bool = False) -> dict:
    """Commit the hash, remote identity and local snapshot after publication."""
    uploaded_version = inspect_version(Path(pending_path), TOKEN_BOARD_DATABASE_NAME)
    set_sync_state_many(db_path, {
        "config_hash": config_hash_of_db(pending_path),
        "remote_artifact": published.name,
        "remote_etag": published.etag or "",
        "sync_health": "ok",
    })
    record_remote_metadata(
        db_path, TOKEN_BOARD_DATABASE_NAME, file_checksum(Path(pending_path)),
        uploaded_version.major if uploaded_version else None,
        uploaded_version.minor if uploaded_version else None)
    snapshot_config(db_path)
    Path(pending_path).unlink(missing_ok=True)
    clear_sync_state_many(db_path, _CONFIG_PENDING_KEYS)
    return {
        "status": "ok",
        "message": "配置已上传",
        "conflict": False,
        "recovered": recovered,
    }


def _recover_config_pending(db_path: str, config: SyncConfig,
                            schema_dir: str | None = None) -> dict | None:
    """Reconcile an interrupted config publication before new sync work."""
    pending_path = get_sync_state(db_path, "config_pending_path")
    pending_name = get_sync_state(db_path, "config_pending_remote_artifact")
    if not pending_path and not pending_name:
        return None
    if not pending_path or not os.path.exists(pending_path):
        error = FileNotFoundError(
            f"durable config artifact is missing: {pending_path or '<unset>'}")
        _mark_sync_degraded(db_path, "config recovery", error)
        return {"status": "error", "message": str(error), "conflict": False,
                "pending": True}

    published = find_artifact(config, pending_name) if pending_name else None
    if published is None:
        expected = latest_artifact(config, "token-board_config")
        published = publish_versioned_artifact(
            config, pending_path, "token-board_config", expected)
        set_sync_state_many(db_path, {
            "config_pending_remote_artifact": published.name,
            "config_pending_remote_etag": published.etag or "",
        })
    publish_schema_manifest(config, pending_path, "token-board_config")
    return _commit_config_pending(db_path, pending_path, published, recovered=True)

def _sync_config_upload_once(db_path: str, schema_dir: str | None = None) -> dict:
    """Upload local config to cloud as one conflict-checked transaction.

    Returns {status: 'ok'|'conflict'|'error', message, conflict}.
    The uploaded file contains synchronized configuration and client_keys.
    Upstream secret values and the WebDAV password are removed from the copy;
    generated runtime tables are also removed. On success the local snapshot
    and the sanitized config hash are updated (commit point).
    """
    config = load_sync_config(db_path)
    if not config:
        return {"status": "unconfigured", "message": "未配置同步服务器", "conflict": False}

    recovered = _recover_config_pending(db_path, config, schema_dir)
    if recovered is not None:
        return recovered

    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    config_path = str(tmp_dir / "token-board_config.db")
    remote_path = str(tmp_dir / "token-board_config_remote.db")

    try:
        # ── 1. Conflict check: refuse if the cloud moved past our last sync. ──
        remote_artifact = latest_artifact(config, "token-board_config")
        has_remote = bool(remote_artifact and download_artifact(
            config, remote_path, remote_filename=remote_artifact.name))
        if not remote_artifact:
            has_remote = False
        if has_remote:
            # Hash on the same schema basis as sync_config_download: the cloud
            # copy may predate the current migration (e.g. still carrying a
            # dropped column like cancellation_grace_hours), which would make
            # its row-hash differ from the stored one purely by schema, not by
            # content — causing a permanent conflict.  Upgrade it here so the
            # comparison is column-consistent.
            remote_version = inspect_version(Path(remote_path), TOKEN_BOARD_DATABASE_NAME)
            local_version = inspect_version(Path(db_path), TOKEN_BOARD_DATABASE_NAME)
            if (remote_version and local_version and
                    remote_version.major not in {0, local_version.major}):
                message = (f"拒绝跨 Major 配置同步: remote=V{remote_version.major}."
                           f"{remote_version.minor}, local=V{local_version.major}."
                           f"{local_version.minor}")
                set_sync_state(db_path, "sync_health", message)
                return {"status": "error", "message": message, "conflict": False}
            upgrade_downloaded_artifact(
                remote_path, TOKEN_BOARD_DATABASE_NAME,
                schema_dir or schema_dir_for(db_path, TOKEN_BOARD_DATABASE_NAME),
                local_token_board_path=db_path, configuration_only=True)
            upgraded_version = inspect_version(Path(remote_path), TOKEN_BOARD_DATABASE_NAME)
            if (upgraded_version and local_version and
                    upgraded_version.major == local_version.major and
                    upgraded_version.minor > local_version.minor):
                message = "云端 schema minor 高于本机，已进入只读兼容模式"
                set_sync_state(db_path, "sync_health", message)
                return {"status": "error",
                        "message": message,
                        "conflict": False}
            last_hash = get_sync_state(db_path, "config_hash")
            cloud_hash = config_hash_of_db(remote_path)
            if last_hash is None or cloud_hash != last_hash:
                return {
                    "status": "conflict",
                    "message": "云端配置已被其他机器修改(或本机尚未下载过),已拒绝覆盖。"
                               "请重启仪表板拉取云端配置合并后再上传。",
                    "conflict": True,
                }

        # ── 2. Build upload copy: keep configuration, drop runtime tables. ──
        safe_copy_db(db_path, config_path)
        dst = sqlite_runtime.connect(config_path, "shadow_copy")
        try:
            for table in RUNTIME_TABLE_DENYLIST:
                if table_exists(dst, table):
                    dst.execute(f"DELETE FROM {table}")
            sanitize_upload_columns(dst)
            dst.commit()
        finally:
            dst.close()
        dst = sqlite_runtime.connect(config_path, "shadow_copy")
        dst.execute("VACUUM")
        dst.close()

        # ── 3. Durable prepare + upload. ──
        pending_path = _prepare_config_pending(db_path, config_path)
        published = publish_versioned_artifact(
            config, pending_path, "token-board_config", remote_artifact)
        set_sync_state_many(db_path, {
            "config_pending_remote_artifact": published.name,
            "config_pending_remote_etag": published.etag or "",
        })
        publish_schema_manifest(config, pending_path, "token-board_config")
        return _commit_config_pending(db_path, pending_path, published)

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

    If the cloud has moved ahead of this machine, the cloud is authoritative:
    refresh the local configuration immediately and report that the local
    unsynchronized edits were discarded.  The caller only gets a conflict
    response when that refresh itself fails.
    """

    def refresh_after_conflict() -> bool:
        try:
            return sync_config_download(db_path, schema_dir=schema_dir)
        except Exception:
            # sync_config_download normally converts failures to False, but
            # keep the upload endpoint recoverable if an unexpected exception
            # escapes its retry boundary.
            log.exception("automatic config refresh after upload conflict failed")
            return False

    last_error = None
    for attempt in range(3):
        try:
            result = _sync_config_upload_once(db_path, schema_dir=schema_dir)
            if result.get("status") != "conflict":
                return result

            # The conflict check already established that a newer immutable
            # cloud artifact exists.  Pull it into the local DB now instead
            # of making the user restart the dashboard manually.
            if refresh_after_conflict():
                return {
                    "status": "remote_updated",
                    "message": "云端配置已更新，本机修改已丢弃，请重新设置。",
                    "conflict": True,
                }
            return {
                **result,
                "message": "云端配置已更新，但自动拉取失败。请重试；若仍失败可丢弃本地设置。",
            }
        except WebDAVConflict as exc:
            last_error = exc
            log.warning("config upload raced with remote update; retry %d/3",
                        attempt + 1)
    if last_error is not None:
        # A race can leave us with a newer remote artifact even though the
        # upload path never reached its ordinary hash-mismatch return.
        if refresh_after_conflict():
            return {
                "status": "remote_updated",
                "message": "云端配置已更新，本机修改已丢弃，请重新设置。",
                "conflict": True,
            }
        _mark_sync_degraded(db_path, "config upload conflict", last_error)
        return {
            "status": "conflict",
            "message": "云端配置已更新，但自动拉取失败。请重试；若仍失败可丢弃本地设置。",
            "conflict": True,
        }
    return {"status": "conflict", "message": str(last_error), "conflict": True}


def _sync_config_download_once(db_path: str,
                               schema_dir: str | None = None) -> bool:
    """Pull the latest cloud config and merge cloud-authoritatively into the
    local DB. On success the snapshot + config hash are updated (commit point)."""
    config = load_sync_config(db_path)
    if not config:
        return False

    recovered = _recover_config_pending(db_path, config, schema_dir)
    if recovered is not None:
        return recovered.get("status") == "ok"

    project_root = Path(db_path).resolve().parent
    tmp_dir = project_root / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    remote_path = str(tmp_dir / "token-board_config_remote.db")

    try:
        remote_artifact = latest_artifact(config, "token-board_config")
        has_remote = bool(remote_artifact and download_artifact(
            config, remote_path, remote_filename=remote_artifact.name))
        if not remote_artifact:
            has_remote = False
        if not has_remote:
            return False
        # Raw identity of the downloaded cloud file, captured before the
        # in-place upgrade below mutates the shadow.
        raw_sha256 = file_checksum(Path(remote_path))
        # The cloud copy is a full-schema snapshot uploaded via _safe_copy_db
        # (backup API) and therefore carries user_version.  Upgrade it to the
        # local schema — exactly like the local DB — before merging, so a cloud
        # file from an older release (e.g. one still carrying a now-dropped
        # column like cancellation_grace_hours) cannot fight the local schema.
        remote_version = inspect_version(Path(remote_path), TOKEN_BOARD_DATABASE_NAME)
        local_version = inspect_version(Path(db_path), TOKEN_BOARD_DATABASE_NAME)
        if (remote_version and local_version and
                remote_version.major not in {0, local_version.major}):
            set_sync_state(db_path, "sync_health", "remote major mismatch")
            return False
        upgrade_downloaded_artifact(
            remote_path, TOKEN_BOARD_DATABASE_NAME,
            schema_dir or schema_dir_for(db_path, TOKEN_BOARD_DATABASE_NAME),
            local_token_board_path=db_path, configuration_only=True)
        upgraded_version = inspect_version(Path(remote_path), TOKEN_BOARD_DATABASE_NAME)
        if (upgraded_version and local_version and
                upgraded_version.major == local_version.major and
                upgraded_version.minor > local_version.minor):
            set_sync_state(db_path, "sync_health", "remote minor is newer; write paused")
            return False
        merge_config_tables(remote_path, db_path)
        if remote_version and remote_version.major == 0:
            publish_path = str(tmp_dir / "token-board_config_v1.db")
            safe_copy_db(db_path, publish_path)
            dst = sqlite_runtime.connect(publish_path, "shadow_copy")
            try:
                sanitize_upload_columns(dst)
                dst.commit()
            finally:
                dst.close()
            published = publish_versioned_artifact(
                config, publish_path, "token-board_config", remote_artifact)
            publish_schema_manifest(config, publish_path, "token-board_config")
            set_sync_state(db_path, "config_hash", config_hash_of_db(publish_path))
            set_sync_state(db_path, "remote_artifact", published.name)
            if published.etag:
                set_sync_state(db_path, "remote_etag", published.etag)
            published_version = inspect_version(Path(publish_path), TOKEN_BOARD_DATABASE_NAME)
            record_remote_metadata(
                db_path, TOKEN_BOARD_DATABASE_NAME, file_checksum(Path(publish_path)),
                published_version.major if published_version else None,
                published_version.minor if published_version else None)
        else:
            set_sync_state(db_path, "config_hash", config_hash_of_db(remote_path))
            if remote_artifact:
                set_sync_state(db_path, "remote_artifact", remote_artifact.name)
                if remote_artifact.etag:
                    set_sync_state(db_path, "remote_etag", remote_artifact.etag)
            record_remote_metadata(
                db_path, TOKEN_BOARD_DATABASE_NAME, raw_sha256,
                remote_version.major if remote_version else None,
                remote_version.minor if remote_version else None)
        snapshot_config(db_path)
        set_sync_state(db_path, "sync_health", "ok")
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
