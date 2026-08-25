"""User-level operations for the usage dashboard archive."""

from __future__ import annotations

from app.db.dashboard_db import DashboardDatabase
from app.services.sync import (
    download_dashboard_from_cloud,
    export_dashboard_to_local,
    load_sync_config,
    upload_dashboard_to_cloud,
)


def prepare_dashboard_user_delete(db_path: str, dash_db_path: str,
                                  schema_dir: str | None = None) -> dict:
    """Prepare the local archive before the first delete in a picker session.

    The operation is deliberately split into reusable download and local export
    steps. Neither step uploads anything; the picker uploads only after it is
    closed.
    """
    if load_sync_config(db_path) is not None:
        downloaded = download_dashboard_from_cloud(
            db_path, dash_db_path, schema_dir=schema_dir)
        if downloaded.get("status") != "ok":
            return downloaded

    exported = export_dashboard_to_local(
        db_path, dash_db_path, schema_dir=schema_dir)
    if exported.get("status") != "ok":
        return exported
    if load_sync_config(db_path) is None:
        exported.update({
            "message": "未配置同步服务器，已使用本地看板数据",
            "remote_pulled": False,
        })
    return exported


def delete_dashboard_user_local(db_path: str, dash_db_path: str, name: str,
                                schema_dir: str | None = None,
                                prepare: bool = False) -> dict:
    """Remove one user's archive from the local Dashboard database only.

    Cloud synchronization is deliberately deferred so the "more users"
    picker can remove several identities in one local batch. If ``prepare`` is
    true, refresh the local archive from cloud first, without uploading; this
    is used only for the first deletion in a picker session.
    """
    clean_name = str(name or "").strip()
    if not clean_name:
        return {"status": "error", "message": "用户名称不能为空"}

    if prepare:
        prepared = prepare_dashboard_user_delete(
            db_path, dash_db_path, schema_dir=schema_dir)
        if prepared.get("status") != "ok":
            return prepared

    dashboard = DashboardDatabase(dash_db_path, schema_dir=schema_dir)
    account_ids = set(dashboard.get_account_ids_by_name(clean_name))
    if not account_ids:
        return {"status": "not_found", "message": "未找到该用户的看板数据"}

    deleted_rows = dashboard.purge_accounts(account_ids)
    return {
        "status": "ok",
        "message": f"用户「{clean_name}」已从本机看板移除",
        "deleted_rows": deleted_rows,
        "account_ids": sorted(account_ids),
        "pending_upload": True,
        "prepared": prepare,
    }


def upload_dashboard_user_deletions(
        db_path: str, dash_db_path: str,
        schema_dir: str | None = None) -> dict:
    """Upload the already-modified local Dashboard archive."""
    return upload_dashboard_to_cloud(
        db_path, dash_db_path, schema_dir=schema_dir)
