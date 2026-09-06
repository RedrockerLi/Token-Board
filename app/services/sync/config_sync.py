"""Configuration synchronization workflow.

Configuration sync deliberately has a smaller contract than dashboard export:
the operator starts one dashboard session, pulls the cloud baseline, edits it,
and publishes the resulting snapshot. A successful WebDAV PUT is the commit
point; directory-listing confirmation and durable upload recovery are not part
of this workflow.
"""

from __future__ import annotations

import logging
import sqlite3
import shutil
import tempfile
from pathlib import Path

from app.core import sqlite_runtime
from app.db.schema_upgrade import upgrade_downloaded_artifact
from app.db.schema_upgrade.coordinator import inspect_version
from app.db.migrations import TOKEN_BOARD_DATABASE_NAME, schema_dir_for
from app.services.sync.common import RUNTIME_TABLE_DELETE_ORDER
from app.services.sync.config_merge import merge_config_tables, sanitize_upload_columns
from app.services.sync.settings import SyncConfig, load_sync_config
from app.services.sync.snapshot import restore_config_snapshot, snapshot_config
from app.services.sync.state import (
    clear_sync_state_many,
    get_sync_state,
    record_remote_metadata,
    set_sync_state,
    table_exists,
)
from app.services.sync.storage import safe_copy_db
from app.services.sync.webdav import (
    RemoteArtifact,
    WebDAVError,
    download_artifact,
    file_checksum,
    latest_artifact,
    publish_config_artifact,
)

log = logging.getLogger(__name__)

_CONFIG_PENDING_KEYS = (
    "config_pending_path",
    "config_pending_remote_artifact",
    "config_pending_remote_etag",
    "config_hash",
    "remote_etag",
)


def _mark_sync_degraded(db_path: str, operation: str, exc: Exception) -> None:
    """Persist a concise diagnostic without masking the original failure."""
    message = f"{operation} degraded: {type(exc).__name__}: {exc}"
    try:
        set_sync_state(db_path, "sync_health", message)
    except Exception:
        log.exception("failed to persist sync health for %s", operation)


def _pending_config_path(db_path: str) -> str:
    """Return the legacy pending path for migration cleanup/tests."""
    path = Path(db_path)
    return str(path.with_name(f".{path.stem}.pending{path.suffix}"))


def _clear_legacy_pending(db_path: str) -> None:
    """Discard old durable config-upload state before every new pull.

    Only paths inside the database directory with the exact legacy pending
    naming convention are removed. A malformed state row must never turn
    startup cleanup into an arbitrary-path delete.
    """
    root = Path(db_path).resolve().parent
    candidates = {_pending_config_path(db_path)}
    try:
        stored = get_sync_state(db_path, "config_pending_path")
        if stored:
            candidate = Path(stored).resolve()
            if (candidate.parent == root and candidate.name.startswith(".")
                    and ".pending" in candidate.name):
                candidates.add(str(candidate))
    except Exception:
        log.exception("failed to inspect legacy config pending state")

    recovery = root / "sync-recovery"
    if recovery.is_dir():
        candidates.update(str(path) for path in recovery.glob(
            ".token-board.pending.db.*.bak"))
    for candidate in candidates:
        try:
            Path(candidate).unlink(missing_ok=True)
        except OSError:
            log.warning("unable to remove legacy config pending file: %s", candidate)
    clear_sync_state_many(db_path, _CONFIG_PENDING_KEYS)


def _build_upload_copy(db_path: str, destination: Path) -> None:
    """Create the sanitized cloud representation of the local database."""
    safe_copy_db(db_path, str(destination))
    conn = sqlite_runtime.connect(destination, "shadow_copy")
    try:
        with sqlite_runtime.transaction(conn, "immediate"):
            for table in RUNTIME_TABLE_DELETE_ORDER:
                if table_exists(conn, table):
                    conn.execute(f"DELETE FROM {table}")
            sanitize_upload_columns(conn)
            violation = conn.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise sqlite3.IntegrityError(
                    f"V2 config upload FK violation: {tuple(violation)}")
    finally:
        conn.close()
    conn = sqlite_runtime.connect(destination, "shadow_copy")
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def _record_authority(db_path: str, source_path: Path,
                      artifact: RemoteArtifact | None = None) -> None:
    """Advance the local rollback baseline after a successful cloud commit."""
    snapshot_config(db_path)
    version = inspect_version(source_path, TOKEN_BOARD_DATABASE_NAME)
    record_remote_metadata(
        db_path,
        TOKEN_BOARD_DATABASE_NAME,
        file_checksum(source_path),
        version.major if version else None,
        version.minor if version else None,
    )
    if artifact is not None:
        set_sync_state(db_path, "remote_artifact", artifact.name)
        if artifact.etag:
            set_sync_state(db_path, "remote_etag", artifact.etag)
    set_sync_state(db_path, "sync_health", "ok")


def _upload_current(db_path: str, config: SyncConfig,
                    *, schema_dir: str | None = None) -> dict:
    """Publish the current local configuration and advance its baseline."""
    del schema_dir  # retained for callers that pass the old public argument
    project_root = Path(db_path).resolve().parent
    tmp_dir = Path(tempfile.mkdtemp(prefix=".config-upload-", dir=project_root))
    config_path = tmp_dir / "token-board_config.db"
    try:
        _build_upload_copy(db_path, config_path)
        published = publish_config_artifact(config, str(config_path))
        try:
            _record_authority(db_path, config_path, published)
            version = inspect_version(config_path, TOKEN_BOARD_DATABASE_NAME)
            if version and version.major >= 2:
                set_sync_state(db_path, "token_board_v2_manifest", "1")
        except Exception:
            # The remote PUT is already authoritative. Never restore the old
            # snapshot after a successful PUT; force a fresh pull instead.
            set_sync_state(
                db_path, "sync_health",
                "config upload committed; local baseline refresh required")
            raise
        return {"status": "ok", "message": "配置已上传"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_upload(db_path: str, schema_dir: str | None = None,
                       *, config: SyncConfig | None = None) -> dict:
    """Upload configuration with PUT acknowledgement as the commit point."""
    config = config or load_sync_config(db_path)
    if not config:
        return {"status": "unconfigured", "message": "未配置同步服务器"}
    try:
        return _upload_current(db_path, config, schema_dir=schema_dir)
    except Exception as exc:
        log.exception("config upload failed")
        try:
            if not restore_config_snapshot(db_path):
                raise FileNotFoundError("没有可回滚的配置基线")
            set_sync_state(db_path, "sync_health", "config upload rolled back")
            return {
                "status": "rolled_back",
                "message": f"上传失败，已恢复云端基线: {type(exc).__name__}: {exc}",
            }
        except Exception as rollback_exc:
            _mark_sync_degraded(db_path, "config upload rollback", rollback_exc)
            return {
                "status": "error",
                "message": f"上传失败且无法恢复云端基线: {type(rollback_exc).__name__}: {rollback_exc}",
            }


def _pull_current(db_path: str, config: SyncConfig,
                  schema_dir: str | None = None) -> dict:
    """Download and merge one cloud artifact, returning a typed result."""
    project_root = Path(db_path).resolve().parent
    tmp_dir = Path(tempfile.mkdtemp(prefix=".config-pull-", dir=project_root))
    remote_path = tmp_dir / "token-board_config_remote.db"
    try:
        remote_artifact = latest_artifact(config, "token-board_config")
        if remote_artifact is None:
            return {"status": "empty", "message": "云端尚无配置文件"}
        if not download_artifact(config, str(remote_path),
                                 remote_filename=remote_artifact.name):
            return {"status": "error", "message": "云端配置文件下载失败"}

        raw_sha256 = file_checksum(remote_path)
        remote_version = inspect_version(remote_path, TOKEN_BOARD_DATABASE_NAME)
        local_version = inspect_version(Path(db_path), TOKEN_BOARD_DATABASE_NAME)
        if (remote_version and local_version and
                remote_version.major > local_version.major):
            raise WebDAVError(
                f"拒绝跨 Major 配置同步: remote=V{remote_version.major}.{remote_version.minor}, "
                f"local=V{local_version.major}.{local_version.minor}")
        if (remote_version and local_version and local_version.major >= 2 and
                remote_version.major < 2 and
                get_sync_state(db_path, "token_board_v2_manifest") == "1"):
            raise WebDAVError("V2 节点拒绝 V2 manifest 发布后的 V1 配置产物")
        upgrade_result = upgrade_downloaded_artifact(
            str(remote_path), TOKEN_BOARD_DATABASE_NAME,
            schema_dir or schema_dir_for(db_path, TOKEN_BOARD_DATABASE_NAME),
            local_token_board_path=db_path, configuration_only=True)
        upgraded_version = inspect_version(remote_path, TOKEN_BOARD_DATABASE_NAME)
        if (upgraded_version and local_version and
                upgraded_version.major == local_version.major and
                upgraded_version.minor > local_version.minor):
            raise WebDAVError("云端 schema minor 高于本机，当前配置保持只读")

        merge_config_tables(str(remote_path), db_path)
        if upgrade_result.upgraded:
            # Publish any upgraded artifact (V0 or same-major V1) as a new
            # immutable gzip artifact. The old remote file is never changed.
            publish_path = tmp_dir / (
                "token-board_config_v2.db"
                if upgraded_version and upgraded_version.major >= 2
                else "token-board_config_v1.db")
            _build_upload_copy(db_path, publish_path)
            published = publish_config_artifact(config, str(publish_path))
            _record_authority(db_path, publish_path, published)
            if upgraded_version and upgraded_version.major >= 2:
                set_sync_state(db_path, "token_board_v2_manifest", "1")
        else:
            _record_authority(db_path, remote_path, remote_artifact)
            # Record raw remote identity, not upgraded shadow bytes.
            record_remote_metadata(
                db_path, TOKEN_BOARD_DATABASE_NAME, raw_sha256,
                remote_version.major if remote_version else None,
                remote_version.minor if remote_version else None)
            if remote_version and remote_version.major >= 2:
                set_sync_state(db_path, "token_board_v2_manifest", "1")
        return {
            "status": "pulled",
            "message": "已拉取云端配置",
            "remote_artifact": remote_artifact.name,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def sync_config_pull(db_path: str, schema_dir: str | None = None,
                     *, config: SyncConfig | None = None) -> dict:
    """Clear legacy state and pull the current cloud baseline once."""
    _clear_legacy_pending(db_path)
    config = config or load_sync_config(db_path)
    if not config:
        snapshot_config(db_path)
        set_sync_state(db_path, "sync_health", "unconfigured")
        return {"status": "unconfigured", "message": "未配置同步服务器"}
    try:
        result = _pull_current(db_path, config, schema_dir=schema_dir)
        if result["status"] in {"pulled", "empty"}:
            return result
        error = WebDAVError(result.get("message", "云端配置拉取失败"))
        _mark_sync_degraded(db_path, "config download", error)
        return {"status": "error", "message": result["message"]}
    except Exception as exc:
        log.exception("config download failed")
        _mark_sync_degraded(db_path, "config download", exc)
        return {"status": "error", "message": f"云端配置拉取失败: {type(exc).__name__}: {exc}"}


def sync_config_download(db_path: str, schema_dir: str | None = None) -> bool:
    """Backward-compatible boolean wrapper around ``sync_config_pull``."""
    return sync_config_pull(db_path, schema_dir=schema_dir)["status"] == "pulled"


__all__ = [
    "sync_config_upload",
    "sync_config_pull",
    "sync_config_download",
    "_pending_config_path",
]
