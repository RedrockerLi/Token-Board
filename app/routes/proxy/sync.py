"""Functional proxy API route group for dashboard and configuration sync."""

from app.routes.contract import status_for
from app.routes.proxy.common import (
    api_error,
    bp_proxy,
    config_session,
    current_app,
    jsonify,
    request,
    require_json_object,
)


@bp_proxy.route("/export", methods=["POST"])
def export_data():
    """Export unexported request_log → dashboard.db, then sync to cloud."""
    import os as _os
    from app.services.sync.dashboard_sync import sync_dashboard

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    dash_db_path = _os.path.join(_os.path.dirname(db_path), "dashboard.db")
    result = sync_dashboard(
        db_path, dash_db_path,
        schema_dir=current_app.config.get("SCHEMA_DIR"),
    )
    ds = current_app.config.get("DATA_STORE")
    if ds and result.get("status") == "ok":
        ds.load()
    return jsonify(result)


@bp_proxy.route("/dashboard/users", methods=["DELETE"])
def delete_dashboard_users():
    """Delete several users in one complete dashboard archive transaction."""
    data = request.get_json(silent=True) or {}
    names = data.get("names")
    import os as _os
    from app.services.sync.dashboard_sync import delete_dashboard_users as _delete_users

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    dash_db_path = _os.path.join(_os.path.dirname(db_path), "dashboard.db")
    result = _delete_users(
        db_path, dash_db_path, names,
        schema_dir=current_app.config.get("SCHEMA_DIR"),
    )
    ds = current_app.config.get("DATA_STORE")
    if ds and result.get("status") == "ok":
        ds.load()
    return jsonify(result), status_for("dashboard_delete", result)


@bp_proxy.route("/sync/config", methods=["GET"])
def get_sync_config():
    from app.services.sync.settings import load_sync_config

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    cfg = load_sync_config(db_path)
    if cfg:
        return jsonify({
            "base_url": cfg.base_url,
            "folder": cfg.folder,
            "username": cfg.username,
            "password": "••••••" if cfg.password else "",
            "has_password": bool(cfg.password),
        })
    return jsonify({
        "base_url": "", "folder": "token-board-sync", "username": "",
        "password": "", "has_password": False,
    })


@bp_proxy.route("/sync/config/status", methods=["GET"])
def get_sync_status():
    session = config_session()
    if session is None:
        return jsonify({"state": "local_only", "writable": True,
                        "message": None, "remote_artifact": None})
    status = session.status()
    return jsonify({
        "state": status.state,
        "writable": status.writable,
        "message": status.message,
        "remote_artifact": status.remote_artifact,
    })


@bp_proxy.route("/sync/config/pull", methods=["POST"])
def retry_sync_config_pull():
    session = config_session()
    if session is None:
        return jsonify({"status": "unconfigured", "message": "未配置同步服务器"})
    session.trigger_pull()
    return jsonify({"status": "syncing", "message": "正在拉取云端配置"}), 202


@bp_proxy.route("/sync/config", methods=["PUT"])
def save_sync_config():
    """Test a candidate WebDAV target and establish its cloud baseline."""
    from app.services.sync.settings import SyncConfig, load_sync_config

    data = require_json_object(force=True)
    if not data.get("base_url") or not data.get("username"):
        return api_error("base_url and username are required", 400)
    if not data["base_url"].startswith(("https://", "http://")):
        return api_error("服务器地址必须以 https:// 或 http:// 开头", 400)
    if "/" not in data["base_url"][8:]:
        return api_error("服务器地址格式不正确，需包含主机名", 400)

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    password = data.get("password", "")
    if password.startswith("••••"):
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""
    candidate = SyncConfig(
        base_url=data["base_url"],
        folder=data.get("folder", "token-board-sync"),
        username=data["username"],
        password=password,
    )
    session = config_session()
    if session is None:
        return api_error("配置同步会话不可用", 503)
    result = session.switch_endpoint(candidate)
    code = 200 if result.get("status") in {"pulled", "seeded"} else 502
    return jsonify(result), code


@bp_proxy.route("/sync/test", methods=["POST"])
def test_sync_connection():
    from app.services.sync.settings import SyncConfig, load_sync_config
    from app.services.sync.webdav import WebDAVClient

    data = require_json_object(force=True)
    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    base_url = data.get("base_url")
    folder = data.get("folder", "token-board-sync")
    username = data.get("username")
    password = data.get("password", "")
    if password.startswith("••••"):
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""
    if not base_url or not username:
        return api_error("base_url and username are required", 400)
    if not base_url.startswith(("https://", "http://")):
        return api_error("服务器地址必须以 https:// 或 http:// 开头", 400)
    cfg = SyncConfig(base_url=base_url, folder=folder,
                     username=username, password=password)
    err = WebDAVClient(cfg).test_connection()
    if err:
        return jsonify({"status": "error", "message": f"连接失败: {err}"}), 400
    return jsonify({"status": "ok", "message": "连接成功"})


@bp_proxy.route("/sync/config/upload", methods=["POST"])
def upload_config():
    session = config_session()
    if session is None:
        return jsonify({"status": "unconfigured", "message": "未配置同步服务器"})
    result = session.upload()
    status = result.get("status")
    code = 502 if status in {"rolled_back", "error"} else 200
    if status == "read_only":
        code = 423
    return jsonify(result), code
