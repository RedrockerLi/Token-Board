"""Dashboard user archive workflow."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.services.sync.webdav import WebDAVConflict, WebDAVError

log = logging.getLogger(__name__)
_DASHBOARD_TRANSACTION_LOCK = threading.RLock()
DashboardTransform = Callable[[str, str], dict]


def _normalise_dashboard_user_ids(user_ids) -> list[int] | None:
    if not isinstance(user_ids, (list, tuple)):
        return None
    try:
        values = {int(value) for value in user_ids}
    except (TypeError, ValueError):
        return None
    if any(value < 0 for value in values):
        return None
    # id=0 is the immutable archive and cannot be archived again.
    if 0 in values:
        return None
    return sorted(values)


def _archive_dashboard_users_transform(user_ids: list[int]) -> DashboardTransform:
    def transform(candidate_path: str, resolved_schema_dir: str) -> dict:
        from app.db.dashboard_db import DashboardDatabase
        dashboard = DashboardDatabase(candidate_path, resolved_schema_dir)
        existing = set(dashboard.get_user_ids())
        found = [user_id for user_id in user_ids if user_id in existing and user_id != 0]
        missing = [user_id for user_id in user_ids if user_id not in existing]
        if not found:
            return {
                "status": "not_found",
                "message": "未找到指定用户的看板数据",
                "archived_user_ids": [],
                "not_found_user_ids": missing,
                "archived_rows": 0,
            }
        archived_rows = dashboard.archive_users(found)
        return {
            "status": "ok",
            "archived_user_ids": found,
            "not_found_user_ids": missing,
            "archived_rows": archived_rows,
        }
    return transform


def delete_dashboard_users(token_board_db_path: str, dash_db_path: str,
                           user_ids, schema_dir: str | None = None) -> dict:
    """Archive user IDs in one cloud-authoritative dashboard transaction."""
    from app.services.sync.dashboard_sync import (
        _discard_unpublished_dashboard_pending,
        _mark_sync_degraded,
        _run_dashboard_transaction_once,
    )

    clean_ids = _normalise_dashboard_user_ids(user_ids)
    if clean_ids is None or not clean_ids:
        return {"status": "invalid", "message": "用户 ID 列表不能为空"}
    last_error = None
    with _DASHBOARD_TRANSACTION_LOCK:
        for attempt in range(3):
            try:
                return _run_dashboard_transaction_once(
                    token_board_db_path, dash_db_path, schema_dir,
                    _archive_dashboard_users_transform(clean_ids),
                    operation="archive")
            except WebDAVConflict as exc:
                last_error = exc
                log.warning("dashboard archive raced with remote update; retry %d/3",
                            attempt + 1)
                _discard_unpublished_dashboard_pending(token_board_db_path)
            except WebDAVError as exc:
                _mark_sync_degraded(token_board_db_path, "dashboard archive", exc)
                return {"status": "error", "message": f"WebDAV 错误: {exc}"}
            except Exception as exc:
                log.exception("dashboard user archive failed")
                _mark_sync_degraded(token_board_db_path, "dashboard archive", exc)
                return {
                    "status": "error",
                    "message": f"归档失败: {type(exc).__name__}: {exc}",
                }
    return {"status": "conflict", "message": str(last_error)}
