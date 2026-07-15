"""Flask Blueprint for proxy management API endpoints.

Mounts at /api/proxy/ — provides CRUD for accounts, keys, pricing,
and read-only billing/usage endpoints backed by the SQLite database
shared with the C++ proxy.
"""

from flask import Blueprint, current_app, jsonify, request

bp_proxy = Blueprint("proxy", __name__, url_prefix="/api/proxy")


def _proxy_db():
    """Return the ProxyDatabase instance from app config."""
    return current_app.config["PROXY_DB"]


# ── Stats ──────────────────────────────────────────────────────────────

@bp_proxy.route("/stats")
def stats():
    return jsonify(_proxy_db().get_stats())


# ── Accounts CRUD ──────────────────────────────────────────────────────

@bp_proxy.route("/accounts", methods=["GET"])
def list_accounts():
    return jsonify(_proxy_db().get_accounts())


@bp_proxy.route("/accounts", methods=["POST"])
def create_account():
    data = request.get_json(force=True)
    if not data.get("name") or not data.get("upstream_key"):
        return jsonify({"error": "name and upstream_key are required"}), 400
    try:
        account_id = _proxy_db().create_account(data)
        return jsonify({"id": account_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    data = request.get_json(force=True)
    ok = _proxy_db().update_account(account_id, data)
    if not ok:
        return jsonify({"error": "No fields to update or account not found"}), 400
    return jsonify({"status": "ok"})


@bp_proxy.route("/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    result = _proxy_db().delete_account(account_id)
    if not result["ok"]:
        return jsonify({"error": result["error"] or "Account not found"}), 400
    return jsonify({"status": "ok"})


# ── Account Models ───────────────────────────────────────────────────

@bp_proxy.route("/accounts/<int:account_id>/models", methods=["POST"])
def update_account_models(account_id):
    """Fetch models from upstream and store them for this account."""
    import requests
    from requests.auth import HTTPBasicAuth

    acc = _proxy_db().get_accounts()
    acc = [a for a in acc if a["id"] == account_id]
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    acc = acc[0]

    try:
        url = acc["base_url"].rstrip("/") + "/models"
        resp = requests.get(
            url,
            headers={"Authorization": "Bearer " + acc["upstream_key"]},
            timeout=15,
        )
        if not resp.ok:
            return jsonify({"error": f"Upstream HTTP {resp.status_code}: {resp.text[:200]}"}), 400
        data = resp.json()
        models = [m["id"] for m in data.get("data", [])]
        count = _proxy_db().update_account_models(account_id, models)
        return jsonify({"status": "ok", "count": count, "models": models})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/accounts/<int:account_id>/models", methods=["GET"])
def get_account_models(account_id):
    return jsonify(_proxy_db().get_account_models(account_id))


# ── Key Model Map ─────────────────────────────────────────────────────

@bp_proxy.route("/keys/<int:key_id>/model-map", methods=["PUT"])
def update_key_model_map(key_id):
    data = request.get_json(force=True)
    count = _proxy_db().update_key_model_map(key_id, data.get("mappings", []))
    return jsonify({"status": "ok", "count": count})


@bp_proxy.route("/keys/<int:key_id>/model-map", methods=["GET"])
def get_key_model_map(key_id):
    return jsonify(_proxy_db().get_key_model_map(key_id))


# ── Keys CRUD ──────────────────────────────────────────────────────────

@bp_proxy.route("/keys", methods=["GET"])
def list_keys():
    keys = _proxy_db().get_keys()
    # Mask the key values for display: show prefix + suffix only
    for k in keys:
        kv = k.get("key_value", "")
        if len(kv) > 12:
            k["key_masked"] = kv[:6] + "..." + kv[-4:]
        else:
            k["key_masked"] = kv[:6] + "..." if len(kv) > 6 else kv
    return jsonify(keys)


@bp_proxy.route("/keys", methods=["POST"])
def create_key():
    data = request.get_json(force=True)
    if not data.get("account_id"):
        return jsonify({"error": "account_id is required"}), 400
    try:
        key_value = _proxy_db().create_key(data)
        return jsonify({"key_value": key_value}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/keys/<int:key_id>", methods=["PUT"])
def update_key(key_id):
    data = request.get_json(force=True)
    ok = _proxy_db().update_key(key_id, data)
    if not ok:
        return jsonify({"error": "No fields to update or key not found"}), 400
    return jsonify({"status": "ok"})


@bp_proxy.route("/keys/<int:key_id>", methods=["DELETE"])
def delete_key(key_id):
    try:
        ok = _proxy_db().delete_key(key_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    if not ok:
        return jsonify({"error": "Key not found"}), 404
    return jsonify({"status": "ok"})


# ── Model Map Templates CRUD ──────────────────────────────────────

@bp_proxy.route("/templates", methods=["GET"])
def list_templates():
    return jsonify(_proxy_db().get_templates())

@bp_proxy.route("/templates", methods=["POST"])
def create_template():
    data = request.get_json(force=True)
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 400
    tid = _proxy_db().create_template(data)
    return jsonify({"id": tid}), 201

@bp_proxy.route("/templates/<int:tid>", methods=["PUT"])
def update_template(tid):
    data = request.get_json(force=True)
    ok = _proxy_db().update_template(tid, data)
    if not ok: return jsonify({"error": "Template not found"}), 404
    return jsonify({"status": "ok"})

@bp_proxy.route("/templates/<int:tid>", methods=["DELETE"])
def delete_template(tid):
    try:
        ok = _proxy_db().delete_template(tid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    if not ok: return jsonify({"error": "Template not found"}), 404
    return jsonify({"status": "ok"})

@bp_proxy.route("/templates/<int:tid>/entries/reorder", methods=["POST"])
def reorder_template_entries(tid):
    data = request.get_json(force=True)
    ok = _proxy_db().reorder_template_entries(tid, data["entry_id"], data["direction"])
    if not ok: return jsonify({"error": "Entry not found"}), 404
    return jsonify({"status": "ok"})


# ── Model Pricing CRUD ─────────────────────────────────────────────────

@bp_proxy.route("/pricing", methods=["GET"])
def list_pricing():
    return jsonify(_proxy_db().get_pricing())


@bp_proxy.route("/pricing", methods=["POST"])
def create_pricing():
    data = request.get_json(force=True)
    if not data.get("model_pattern"):
        return jsonify({"error": "model_pattern is required"}), 400
    try:
        pid = _proxy_db().create_pricing(data)
        return jsonify({"id": pid}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/pricing/<int:pricing_id>", methods=["PUT"])
def update_pricing(pricing_id):
    data = request.get_json(force=True)
    ok = _proxy_db().update_pricing(pricing_id, data)
    if not ok:
        return jsonify({"error": "No fields to update or pricing not found"}), 400
    return jsonify({"status": "ok"})


@bp_proxy.route("/pricing/<int:pricing_id>", methods=["DELETE"])
def delete_pricing(pricing_id):
    ok = _proxy_db().delete_pricing(pricing_id)
    if not ok:
        return jsonify({"error": "Pricing entry not found"}), 404
    return jsonify({"status": "ok"})


@bp_proxy.route("/pricing/reorder", methods=["POST"])
def reorder_pricing():
    data = request.get_json(force=True)
    ok = _proxy_db().reorder_pricing(data["id"], data["direction"])
    if not ok:
        return jsonify({"error": "Pricing entry not found"}), 404
    return jsonify({"status": "ok"})


# ── Billing / Usage ────────────────────────────────────────────────────

@bp_proxy.route("/billing")
def billing_summary():
    account_id = request.args.get("account_id", type=int)
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    return jsonify(
        _proxy_db().get_billing_summary(account_id, date_from, date_to)
    )


@bp_proxy.route("/billing/by-account")
def billing_by_account():
    return jsonify(_proxy_db().get_billing_by_account())


@bp_proxy.route("/billing/daily")
def daily_billing():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if not year or not month:
        return jsonify({"error": "year and month are required"}), 400
    return jsonify(_proxy_db().get_daily_billing(year, month))


@bp_proxy.route("/billing/months")
def proxy_months():
    return jsonify(_proxy_db().get_available_proxy_months())


@bp_proxy.route("/export", methods=["POST"])
def export_data():
    """Export all unexported request_log rows to dashboard.db.

    Accepts optional year/month (frontend still sends them, ignored now
    that export is incremental via the exported flag).
    """
    result = _proxy_db().export_to_dashboard()

    # Trigger dashboard data reload
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    return jsonify(result)


# ── WebDAV Sync ──────────────────────────────────────────────────────

@bp_proxy.route("/sync", methods=["POST"])
def sync_database():
    from app.sync import sync_config_download, sync_dashboard

    db_path = current_app.config["PROXY_DB"].db_path
    result = {"status": "ok", "message": "同步开始"}

    # Sync proxy config from cloud (INSERT OR IGNORE: local wins, new rows merged)
    try:
        if sync_config_download(db_path):
            result["config_sync"] = "配置已从云端更新"
        else:
            result["config_sync"] = "跳过 (无远程配置或未变化)"
    except Exception as e:
        import traceback
        traceback.print_exc()
        result["config_sync"] = f"配置同步失败: {e}"

    # Sync dashboard database (pull-export-push)
    import os as _os
    dash_db_path = _os.path.join(_os.path.dirname(db_path), "dashboard.db")
    try:
        dash_result = sync_dashboard(db_path, dash_db_path)
        if dash_result.get("status") == "error":
            print(f"[Sync] Dashboard sync error: {dash_result.get('message')}", flush=True)
            result["dashboard_sync"] = "失败: " + dash_result.get("message", "")
        else:
            result["dashboard_sync"] = dash_result.get("message", "")
            result["dashboard_records"] = dash_result.get("dashboard_records", 0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        result["dashboard_sync"] = f"失败: {e}"

    # Reload dashboard data
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    return jsonify(result), 200


@bp_proxy.route("/sync/config", methods=["GET"])
def get_sync_config():
    from app.sync import load_sync_config

    db_path = current_app.config["PROXY_DB"].db_path
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
    from app.sync import SyncConfig, save_sync_config as save_cfg

    data = request.get_json(force=True)
    if not data.get("base_url") or not data.get("username"):
        return jsonify({"error": "base_url and username are required"}), 400
    if not data["base_url"].startswith(("https://", "http://")):
        return jsonify({"error": "服务器地址必须以 https:// 或 http:// 开头"}), 400
    if "/" not in data["base_url"][8:]:
        return jsonify({"error": "服务器地址格式不正确，需包含主机名"}), 400

    db_path = current_app.config["PROXY_DB"].db_path

    # If password is masked placeholder, preserve the existing one
    if data.get("password", "").startswith("••••"):
        from app.sync import load_sync_config
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
    from app.sync import SyncConfig, _webdav_test

    data = request.get_json(force=True)
    db_path = current_app.config["PROXY_DB"].db_path

    # Build config from request (or fall back to saved config)
    base_url = data.get("base_url")
    folder = data.get("folder", "token-board-sync")
    username = data.get("username")
    password = data.get("password", "")

    if password.startswith("••••"):
        from app.sync import load_sync_config
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


@bp_proxy.route("/logs")
def request_logs():
    return jsonify(
        _proxy_db().get_request_logs(
            page=request.args.get("page", 1, type=int),
            per_page=request.args.get("per_page", 50, type=int),
            account_id=request.args.get("account_id", type=int),
            model=request.args.get("model"),
            date_from=request.args.get("from"),
            date_to=request.args.get("to"),
        )
    )


# ── Performance Metrics ────────────────────────────────────────────────

@bp_proxy.route("/perf/summary")
def perf_summary():
    minutes = request.args.get("minutes", 15, type=int)
    return jsonify(_proxy_db().get_perf_summary(minutes))


@bp_proxy.route("/perf/latency")
def perf_latency():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_latency(minutes))


@bp_proxy.route("/perf/throughput")
def perf_throughput():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_throughput(minutes))


@bp_proxy.route("/perf/models")
def perf_models():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_models(minutes))


@bp_proxy.route("/perf/realtime")
def perf_realtime():
    return jsonify(_proxy_db().get_perf_realtime())
