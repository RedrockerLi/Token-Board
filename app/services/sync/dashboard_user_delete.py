"""Dashboard archive user deletion workflow."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.services.sync.webdav import WebDAVConflict, WebDAVError

log = logging.getLogger(__name__)
_DASHBOARD_TRANSACTION_LOCK = threading.RLock()
DashboardTransform = Callable[[str, str], dict]


def _normalise_dashboard_user_names(names) -> list[str] | None:
    if not isinstance(names, (list, tuple)) or any(
            not isinstance(name, str) for name in names):
        return None
    cleaned = {name.strip() for name in names}
    return sorted(name for name in cleaned if name)


def _delete_dashboard_users_transform(names: list[str]) -> DashboardTransform:
    def transform(candidate_path: str, resolved_schema_dir: str) -> dict:
        from app.db.dashboard_db import DashboardDatabase
        dashboard = DashboardDatabase(candidate_path, resolved_schema_dir)
        found: list[str] = []
        missing: list[str] = []
        account_ids: set[int] = set()
        for name in names:
            ids = dashboard.get_account_ids_by_name(name)
            if ids:
                found.append(name)
                account_ids.update(ids)
            else:
                missing.append(name)
        if not found:
            return {
                "status": "not_found",
                "message": "未找到指定用户的看板数据",
                "deleted_names": [],
                "not_found_names": missing,
                "deleted_rows": 0,
            }
        deleted_rows = dashboard.purge_accounts(account_ids)
        return {
            "status": "ok",
            "deleted_names": found,
            "not_found_names": missing,
            "deleted_rows": deleted_rows,
        }
    return transform


def delete_dashboard_users(token_board_db_path: str, dash_db_path: str,
                           names, schema_dir: str | None = None) -> dict:
    """Atomically remove several user archives from the next committed copy."""
    from app.services.sync.dashboard_sync import (
        _discard_unpublished_dashboard_pending,
        _mark_sync_degraded,
        _run_dashboard_transaction_once,
    )

    clean_names = _normalise_dashboard_user_names(names)
    if not clean_names:
        return {"status": "invalid", "message": "用户名称列表不能为空"}
    last_error = None
    with _DASHBOARD_TRANSACTION_LOCK:
        for attempt in range(3):
            try:
                return _run_dashboard_transaction_once(
                    token_board_db_path, dash_db_path, schema_dir,
                    _delete_dashboard_users_transform(clean_names))
            except WebDAVConflict as exc:
                last_error = exc
                log.warning("dashboard delete raced with remote update; retry %d/3",
                            attempt + 1)
                _discard_unpublished_dashboard_pending(token_board_db_path)
            except WebDAVError as exc:
                _mark_sync_degraded(token_board_db_path, "dashboard delete", exc)
                return {"status": "error", "message": f"WebDAV 错误: {exc}"}
            except Exception as exc:
                log.exception("dashboard user delete failed")
                _mark_sync_degraded(token_board_db_path, "dashboard delete", exc)
                return {
                    "status": "error",
                    "message": f"删除失败: {type(exc).__name__}: {exc}",
                }
    return {"status": "conflict", "message": str(last_error)}
