"""Functional WebDAV synchronization module."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core import sqlite_runtime
from app.db.schema_upgrade import upgrade_downloaded_artifact
from app.db.schema_upgrade.coordinator import inspect_version
from app.db.migrations import schema_dir_for

log = logging.getLogger(__name__)

from app.services.sync.webdav import (
    RemoteArtifact,
    WebDAVConflict,
    WebDAVError,
    download_artifact,
    find_artifact,
    file_checksum,
    latest_artifact,
    publish_schema_manifest,
    publish_versioned_artifact,
)
from app.services.sync.settings import SyncConfig, load_sync_config
from app.services.sync.state import (
    clear_sync_state,
    clear_sync_state_many,
    get_sync_state,
    record_remote_metadata,
    set_sync_state,
    set_sync_state_many,
)
from app.services.sync.storage import safe_copy_db
from app.services.sync.dashboard_user_delete import (
    _DASHBOARD_TRANSACTION_LOCK,
    delete_dashboard_users,
)


def _mark_sync_degraded(db_path: str, operation: str, exc: Exception) -> None:
    """Persist a sticky dashboard-sync failure for health/reporting APIs."""
    message = f"{operation} degraded: {type(exc).__name__}: {exc}"
    try:
        set_sync_state(db_path, "sync_health", message)
    except Exception:
        log.exception("failed to persist sync health for %s", operation)
def _count_dashboard_rows(db_path: str) -> int:
    """Count rows in the normalized V1 dashboard archive."""
    conn = sqlite_runtime.connect(db_path, "dashboard_runtime")
    try:
        return (conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0] +
                conn.execute("SELECT COUNT(*) FROM monthly_recurring_costs").fetchone()[0])
    finally:
        conn.close()
def _stored_dashboard_artifact(db_path: str) -> RemoteArtifact | None:
    """Return the remote version observed by the last dashboard download."""
    name = get_sync_state(db_path, "dashboard_remote_artifact")
    if not name:
        return None
    etag = get_sync_state(db_path, "dashboard_remote_etag")
    return RemoteArtifact(name, etag=etag or None)
_DASHBOARD_PENDING_KEYS = (
    "dashboard_pending_path",
    "dashboard_pending_export_max_id",
    "dashboard_pending_remote_artifact",
    "dashboard_pending_remote_etag",
)


@dataclass(frozen=True)
class PreparedDashboard:
    """Durable candidate whose bytes and export boundary move together."""

    path: Path
    max_log_id: int
    checksum: str
    expected_remote: RemoteArtifact | None


def _pending_dashboard_path(dash_db_path: str) -> str:
    """Return the durable sidecar used between remote and local commits."""
    path = Path(dash_db_path)
    return str(path.with_name(f".{path.stem}.pending{path.suffix}"))
def _prepare_pending(token_board_db_path: str, source_path: str,
                     dash_db_path: str, max_id: int,
                     expected_remote: RemoteArtifact | None) -> PreparedDashboard:
    pending_path = _pending_dashboard_path(dash_db_path)
    safe_copy_db(source_path, pending_path)
    set_sync_state_many(token_board_db_path, {
        "dashboard_pending_path": pending_path,
        "dashboard_pending_export_max_id": str(max_id),
        "dashboard_pending_remote_artifact": "",
        "dashboard_pending_remote_etag": "",
    })
    return PreparedDashboard(
        path=Path(pending_path),
        max_log_id=max_id,
        checksum=file_checksum(Path(pending_path)),
        expected_remote=expected_remote,
    )


def _prepared_from_state(token_board_db_path: str) -> PreparedDashboard | None:
    pending_path = get_sync_state(token_board_db_path, "dashboard_pending_path")
    max_value = get_sync_state(
        token_board_db_path, "dashboard_pending_export_max_id")
    if not pending_path and not max_value:
        return None
    if not pending_path or not max_value:
        raise RuntimeError("dashboard pending state is incomplete")
    path = Path(pending_path)
    if not path.exists():
        raise FileNotFoundError(f"durable dashboard export is missing: {path}")
    return PreparedDashboard(
        path=path,
        max_log_id=int(max_value),
        checksum=file_checksum(path),
        expected_remote=_stored_dashboard_artifact(token_board_db_path),
    )


def _discard_unpublished_dashboard_pending(token_board_db_path: str) -> None:
    """Discard only a candidate that has not been confirmed by WebDAV."""
    if not Path(token_board_db_path).exists():
        return
    try:
        pending_name = get_sync_state(
            token_board_db_path, "dashboard_pending_remote_artifact")
    except sqlite3.OperationalError:
        return
    if pending_name:
        return
    pending_path = get_sync_state(token_board_db_path, "dashboard_pending_path")
    if pending_path:
        Path(pending_path).unlink(missing_ok=True)
    clear_sync_state_many(token_board_db_path, _DASHBOARD_PENDING_KEYS)
def _commit_dashboard_pending(token_board_db_path: str, dash_db_path: str,
                              pending_path: str, max_id: int,
                              published: RemoteArtifact | None,
                              schema_dir: str | None,
                              export_result: dict | None = None,
                              *, recovered: bool = False) -> dict:
    """Commit one durable prepared dashboard exactly once.

    The copy is deliberately before the proxy checkpoint and request-log
    cleanup.  Any exception leaves the pending sidecar and markers intact, so
    the next run can repeat this idempotently.
    """
    from app.db.proxy_db import ProxyDatabase
    resolved_schema_dir = schema_dir or schema_dir_for(dash_db_path, "dashboard")
    proxy_db = ProxyDatabase(token_board_db_path, schema_dir=resolved_schema_dir)

    # Local installation is the boundary after which it is safe to advance
    # the source high-water mark.
    safe_copy_db(pending_path, dash_db_path)
    proxy_db.set_export_mark(max_id)
    state = {"sync_health": "ok"}
    if published is not None:
        state.update({
            "dashboard_remote_artifact": published.name,
            "dashboard_remote_etag": published.etag or "",
        })
    set_sync_state_many(token_board_db_path, state)
    if published is not None:
        version = inspect_version(Path(dash_db_path), "dashboard")
        record_remote_metadata(
            token_board_db_path, "dashboard", file_checksum(Path(dash_db_path)),
            version.major if version else None,
            version.minor if version else None)
    cleaned = proxy_db.cleanup_exported_logs(max_id)
    if cleaned > 0:
        log.info("cleaned archived request_log rows: count=%d", cleaned)
    # Remove the sidecar only after every durable local commit step succeeds.
    # If this unlink itself fails, the markers remain and the next recovery
    # repeats the idempotent commit instead of losing the prepared bytes.
    Path(pending_path).unlink(missing_ok=True)
    clear_sync_state_many(token_board_db_path, _DASHBOARD_PENDING_KEYS)
    upload_count = _count_dashboard_rows(dash_db_path)
    if export_result is None:
        message = (f"仪表板：上传 {upload_count} 条至云端" if published
                   else f"仪表板：本地提交 {upload_count} 条")
        exported = 0
    else:
        verb = "上传" if published is not None else "本地提交"
        suffix = "至云端" if published is not None else ""
        message = (
            f"仪表板：导出 {export_result.get('record_count', 0)} 条，"
            f"{verb} {upload_count} 条{suffix}"
        )
        exported = export_result.get("record_count", 0)
    return {
        "status": "ok",
        "message": message,
        "dashboard_records": upload_count,
        "record_count": exported,
        "recovered": recovered,
        "uploaded": published is not None,
    }
def _recover_dashboard_pending(token_board_db_path: str,
                               dash_db_path: str,
                               config: SyncConfig,
                               schema_dir: str | None = None) -> dict | None:
    """Reconcile a durable dashboard commit left by a prior interrupted run."""
    pending_path = get_sync_state(token_board_db_path, "dashboard_pending_path")
    max_value = get_sync_state(
        token_board_db_path, "dashboard_pending_export_max_id")
    pending_name = get_sync_state(
        token_board_db_path, "dashboard_pending_remote_artifact")
    if not pending_path and not max_value and not pending_name:
        return None
    if not pending_path or not max_value:
        error = RuntimeError("dashboard pending state is incomplete")
        _mark_sync_degraded(token_board_db_path, "dashboard recovery", error)
        return {"status": "error", "message": str(error), "pending": True}
    try:
        max_id = int(max_value)
    except (TypeError, ValueError):
        error = ValueError("dashboard pending export mark is invalid")
        _mark_sync_degraded(token_board_db_path, "dashboard recovery", error)
        return {"status": "error", "message": str(error), "pending": True}
    if not os.path.exists(pending_path):
        error = FileNotFoundError(
            f"durable dashboard export is missing: {pending_path}")
        _mark_sync_degraded(token_board_db_path, "dashboard recovery", error)
        return {"status": "error", "message": str(error), "pending": True}

    # A recorded remote name is authoritative only if a fresh PROPFIND still
    # sees that exact file. If it was externally deleted, treat the old PUT as
    # unconfirmed and republish the durable prepared export with a new name.
    published = find_artifact(config, pending_name) if pending_name else None
    if published is None:
        expected = latest_artifact(config, "dashboard_sync")
        published = publish_versioned_artifact(
            config, pending_path, "dashboard_sync", expected)
        set_sync_state_many(token_board_db_path, {
            "dashboard_pending_remote_artifact": published.name,
            "dashboard_pending_remote_etag": published.etag or "",
        })
    # A manifest upload is idempotent and is retried with the prepared bytes
    # before local commit if a prior process died between artifact and marker.
    publish_schema_manifest(config, pending_path, "dashboard_sync")
    return _commit_dashboard_pending(
        token_board_db_path, dash_db_path, pending_path, max_id, published,
        schema_dir, recovered=True)
def _download_dashboard_shadow(token_board_db_path: str, dash_db_path: str,
                               shadow_path: str,
                               schema_dir: str | None,
                               config: SyncConfig) -> dict:
    """Download and validate a dashboard artifact into *shadow_path*.

    This helper only reads the cloud and prepares a local SQLite file. It does
    not export request logs, delete users, upload anything, or advance the
    export checkpoint.
    """
    remote_artifact = latest_artifact(config, "dashboard_sync")
    has_remote = bool(remote_artifact and download_artifact(
        config, shadow_path, remote_filename=remote_artifact.name))
    if not remote_artifact and download_artifact(
            config, shadow_path, remote_filename="dashboard_sync.db"):
        remote_artifact = RemoteArtifact("dashboard_sync.db")
        has_remote = True
    if not has_remote and os.path.exists(dash_db_path):
        safe_copy_db(dash_db_path, shadow_path)
    if not os.path.exists(shadow_path):
        return {"status": "error", "message": "云端和本地都没有 dashboard 存档"}

    raw_sha256 = file_checksum(Path(shadow_path))
    remote_version = inspect_version(Path(shadow_path), "dashboard")
    local_version = (inspect_version(Path(dash_db_path), "dashboard")
                     if os.path.exists(dash_db_path) else None)
    if (remote_version and local_version and
            remote_version.major not in {0, local_version.major}):
        set_sync_state(token_board_db_path, "sync_health", "remote major mismatch")
        return {"status": "error", "message": "云端 dashboard 跨 Major，已暂停同步"}

    resolved_schema_dir = schema_dir or schema_dir_for(
        dash_db_path, "dashboard")
    upgrade_downloaded_artifact(
        shadow_path, "dashboard", resolved_schema_dir,
        local_token_board_path=token_board_db_path)
    upgraded_version = inspect_version(Path(shadow_path), "dashboard")
    if (upgraded_version and local_version and
            upgraded_version.major == local_version.major and
            upgraded_version.minor > local_version.minor):
        set_sync_state(token_board_db_path,
                        "sync_health", "remote minor is newer; write paused")
        return {"status": "error", "message": "云端 dashboard minor 更高，已暂停写入"}

    return {
        "status": "ok",
        "shadow_path": shadow_path,
        "remote_artifact": remote_artifact,
        "remote_pulled": has_remote,
        "raw_sha256": raw_sha256,
        "remote_version": remote_version,
        "resolved_schema_dir": resolved_schema_dir,
    }
def _export_dashboard(token_board_db_path: str, target_path: str,
                      schema_dir: str | None) -> dict:
    """Export local request usage into a dashboard file without uploading."""
    from app.db.dashboard_db import DashboardDatabase
    from app.db.proxy_db import ProxyDatabase
    resolved_schema_dir = schema_dir or schema_dir_for(
        target_path, "dashboard")
    proxy_db = ProxyDatabase(
        token_board_db_path, schema_dir=resolved_schema_dir)
    mark = proxy_db.get_export_mark()
    max_id = proxy_db.get_max_log_id()
    export_result = proxy_db.export_to_dashboard(target_path, mark, max_id)

    # Export only adds rows. Remove all-zero buckets from the local result so
    # the next upload does not publish failed/test model cards.
    dashboard = DashboardDatabase(target_path, schema_dir=resolved_schema_dir)
    purged = dashboard.purge_zero_usage_rows()
    if purged > 0:
        log.info("purged zero-usage archive rows: count=%d", purged)

    return {
        "status": "ok",
        "record_count": export_result.get("record_count", 0),
        "dashboard_records": _count_dashboard_rows(target_path),
        "max_id": max_id,
        "resolved_schema_dir": resolved_schema_dir,
    }
def _publish_prepared(token_board_db_path: str, dash_db_path: str,
                      prepared: PreparedDashboard, config: SyncConfig,
                      schema_dir: str | None,
                      export_result: dict | None = None) -> dict:
    """Publish and commit exactly the candidate described by ``prepared``."""
    if file_checksum(prepared.path) != prepared.checksum:
        raise RuntimeError("prepared dashboard changed before upload")
    published = publish_versioned_artifact(
        config, str(prepared.path), "dashboard_sync",
        prepared.expected_remote)
    set_sync_state_many(token_board_db_path, {
        "dashboard_pending_path": str(prepared.path),
        "dashboard_pending_export_max_id": str(prepared.max_log_id),
        "dashboard_pending_remote_artifact": published.name,
        "dashboard_pending_remote_etag": published.etag or "",
    })
    publish_schema_manifest(config, str(prepared.path), "dashboard_sync")
    return _commit_dashboard_pending(
        token_board_db_path, dash_db_path, str(prepared.path),
        prepared.max_log_id, published, schema_dir, export_result)


def _recover_local_pending(token_board_db_path: str, dash_db_path: str,
                           schema_dir: str | None) -> dict | None:
    """Finish a local-only transaction without guessing remote state."""
    try:
        prepared = _prepared_from_state(token_board_db_path)
    except Exception as exc:
        _mark_sync_degraded(token_board_db_path, "dashboard recovery", exc)
        return {"status": "error", "message": str(exc), "pending": True}
    if prepared is None:
        return None
    remote_name = get_sync_state(
        token_board_db_path, "dashboard_pending_remote_artifact")
    if remote_name:
        error = RuntimeError(
            "dashboard pending upload requires the previous WebDAV configuration")
        _mark_sync_degraded(token_board_db_path, "dashboard recovery", error)
        return {"status": "error", "message": str(error), "pending": True}
    return _commit_dashboard_pending(
        token_board_db_path, dash_db_path, str(prepared.path),
        prepared.max_log_id, None, schema_dir, recovered=True)


def _build_dashboard_candidate(token_board_db_path: str, dash_db_path: str,
                               candidate_path: str, config: SyncConfig | None,
                               schema_dir: str | None) -> dict:
    if config is not None:
        return _download_dashboard_shadow(
            token_board_db_path, dash_db_path, candidate_path,
            schema_dir, config)
    if not os.path.exists(dash_db_path):
        return {"status": "error", "message": "本地 dashboard 存档不存在"}
    safe_copy_db(dash_db_path, candidate_path)
    return {
        "status": "ok",
        "shadow_path": candidate_path,
        "remote_artifact": None,
        "remote_pulled": False,
        "resolved_schema_dir": schema_dir or schema_dir_for(
            dash_db_path, "dashboard"),
    }


DashboardTransform = Callable[[str, str], dict]


def _run_dashboard_transaction_once(
        token_board_db_path: str, dash_db_path: str,
        schema_dir: str | None = None,
        transform: DashboardTransform | None = None) -> dict:
    """Build, optionally transform, publish, and commit one archive."""
    project_root = Path(dash_db_path).resolve().parent
    tmp_dir = project_root / "tmp_dash"
    tmp_dir.mkdir(exist_ok=True)
    config = load_sync_config(token_board_db_path)

    try:
        if config is not None:
            recovered = _recover_dashboard_pending(
                token_board_db_path, dash_db_path, config, schema_dir)
        else:
            recovered = _recover_local_pending(
                token_board_db_path, dash_db_path, schema_dir)
        if recovered is not None and recovered.get("status") != "ok":
            return recovered

        candidate_path = str(tmp_dir / "dash_candidate.db")
        candidate = _build_dashboard_candidate(
            token_board_db_path, dash_db_path, candidate_path,
            config, schema_dir)
        if candidate.get("status") != "ok":
            return candidate

        from app.db.dashboard_db import reconcile_accounts
        reconcile_accounts(candidate_path, token_board_db_path)
        export_result = _export_dashboard(
            token_board_db_path, candidate_path,
            candidate["resolved_schema_dir"])

        transform_result = (transform(candidate_path,
                                      candidate["resolved_schema_dir"])
                            if transform is not None else {"status": "ok"})
        if transform_result.get("status") != "ok":
            return transform_result

        expected = candidate.get("remote_artifact")
        if config is not None and expected is None:
            expected = _stored_dashboard_artifact(token_board_db_path)
            if expected is None:
                expected = latest_artifact(config, "dashboard_sync")
        prepared = _prepare_pending(
            token_board_db_path, candidate_path, dash_db_path,
            export_result["max_id"], expected)

        if config is None:
            result = _commit_dashboard_pending(
                token_board_db_path, dash_db_path, str(prepared.path),
                prepared.max_log_id, None, candidate["resolved_schema_dir"],
                export_result)
        else:
            result = _publish_prepared(
                token_board_db_path, dash_db_path, prepared, config,
                candidate["resolved_schema_dir"], export_result)
        result.update(transform_result)
        result.update({
            "remote_pulled": candidate.get("remote_pulled", False),
            "uploaded": config is not None,
        })
        if transform_result.get("deleted_names"):
            result["message"] = (
                f"已删除 {len(transform_result['deleted_names'])} 个用户的看板数据")
        return result
    finally:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _sync_dashboard_once(token_board_db_path: str, dash_db_path: str,
                         schema_dir: str | None = None) -> dict:
    """Run the normal pull → export → commit transaction once.

    With no WebDAV configuration the same candidate/commit pipeline is kept
    local.  This preserves the export high-water mark semantics without
    pretending that a remote upload happened.
    """
    config = load_sync_config(token_board_db_path)
    try:
        return _run_dashboard_transaction_once(
            token_board_db_path, dash_db_path, schema_dir)
    except WebDAVConflict:
        raise
    except WebDAVError as exc:
        _mark_sync_degraded(token_board_db_path, "dashboard sync", exc)
        return {"status": "error", "message": f"WebDAV 错误: {exc}"}
    except Exception as exc:
        log.exception("dashboard sync failed")
        _mark_sync_degraded(token_board_db_path, "dashboard sync", exc)
        return {
            "status": "error",
            "message": f"同步失败: {type(exc).__name__}: {exc}",
        }


def sync_dashboard(token_board_db_path: str, dash_db_path: str,
                   schema_dir: str | None = None) -> dict:
    """Run the cloud archive transaction with bounded conflict retries."""
    last_error = None
    with _DASHBOARD_TRANSACTION_LOCK:
        for attempt in range(3):
            try:
                return _sync_dashboard_once(
                    token_board_db_path, dash_db_path, schema_dir=schema_dir)
            except WebDAVConflict as exc:
                last_error = exc
                log.warning("dashboard upload raced with remote update; retry %d/3",
                            attempt + 1)
                _discard_unpublished_dashboard_pending(token_board_db_path)
    if last_error is not None:
        _mark_sync_degraded(token_board_db_path, "dashboard sync conflict", last_error)
    return {"status": "conflict", "message": str(last_error)}
