"""Functional proxy API route group."""

from app.routes.proxy.common import *  # noqa: F401,F403

@bp_proxy.route("/export", methods=["POST"])
def export_data():
    """Export unexported request_log → dashboard.db, then sync to cloud.

    Full pipeline: pull remote dashboard → export local → push back.
    """
    import os as _os
    from app.services.sync import sync_dashboard

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    dash_db_path = _os.path.join(_os.path.dirname(db_path), "dashboard.db")

    result = sync_dashboard(
        db_path, dash_db_path,
        schema_dir=current_app.config.get("SCHEMA_DIR"),
    )

    # Trigger dashboard data reload
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    return jsonify(result)


@bp_proxy.route("/sync/config", methods=["GET"])
def get_sync_config():
    from app.services.sync import load_sync_config

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
    return jsonify({"base_url": "", "folder": "token-board-sync", "username": "", "password": "", "has_password": False})


@bp_proxy.route("/sync/config", methods=["PUT"])
def save_sync_config():
    from app.services.sync import SyncConfig, save_sync_config as save_cfg

    data = request.get_json(force=True)
    if not data.get("base_url") or not data.get("username"):
        return jsonify({"error": "base_url and username are required"}), 400
    if not data["base_url"].startswith(("https://", "http://")):
        return jsonify({"error": "服务器地址必须以 https:// 或 http:// 开头"}), 400
    if "/" not in data["base_url"][8:]:
        return jsonify({"error": "服务器地址格式不正确，需包含主机名"}), 400

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path

    # If password is masked placeholder, preserve the existing one
    if data.get("password", "").startswith("••••"):
        from app.services.sync import load_sync_config
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""
    else:
        password = data.get("password", "")

    cfg = SyncConfig(
        base_url=data["base_url"],
        folder=data.get("folder", "token-board-sync"),
        username=data["username"],
        password=password,
    )
    save_cfg(db_path, cfg)
    return jsonify({"status": "ok"})


@bp_proxy.route("/sync/test", methods=["POST"])
def test_sync_connection():
    from app.services.sync import SyncConfig, _webdav_test

    data = request.get_json(force=True)
    db_path = current_app.config["TOKEN_BOARD_DB"].db_path

    # Build config from request (or fall back to saved config)
    base_url = data.get("base_url")
    folder = data.get("folder", "token-board-sync")
    username = data.get("username")
    password = data.get("password", "")

    if password.startswith("••••"):
        from app.services.sync import load_sync_config
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""

    if not base_url or not username:
        return jsonify({"error": "base_url and username are required"}), 400
    if not base_url.startswith(("https://", "http://")):
        return jsonify({"error": "服务器地址必须以 https:// 或 http:// 开头"}), 400

    cfg = SyncConfig(base_url=base_url, folder=folder, username=username, password=password)
    err = _webdav_test(cfg)
    if err:
        return jsonify({"status": "error", "message": f"连接失败: {err}"}), 400
    return jsonify({"status": "ok", "message": "连接成功"})


@bp_proxy.route("/sync/config/upload", methods=["POST"])
def upload_config():
    """Upload local config to cloud as one transaction (exit settings page).

    Uploads all configured proxy state. Request history and other generated
    runtime state remain local. Refuses (conflict) if the cloud moved past
    this machine's last sync.
    """
    from app.services.sync import sync_config_upload

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    result = sync_config_upload(
        db_path, schema_dir=current_app.config.get("SCHEMA_DIR"))
    return jsonify(result)


@bp_proxy.route("/sync/config/discard", methods=["POST"])
def discard_config():
    """Roll local config tables back to the last-committed snapshot.

    Called when the user chooses "丢弃设置" after a failed upload — local
    edits are reverted (including per-machine upstream keys) without network.
    """
    from app.services.sync import restore_config_snapshot

    db_path = current_app.config["TOKEN_BOARD_DB"].db_path
    if not restore_config_snapshot(db_path):
        return jsonify({"status": "error",
                        "message": "没有可回滚的快照(本机尚未成功同步过)"}), 400
    return jsonify({"status": "ok", "message": "已回滚到上次同步状态"})
