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
    ok = _proxy_db().delete_key(key_id)
    if not ok:
        return jsonify({"error": "Key not found"}), 404
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
    data = request.get_json(force=True)
    year = data.get("year")
    month = data.get("month")
    if not year or not month:
        return jsonify({"error": "year and month are required"}), 400

    output_dir = data.get("output_dir", "data/boardproxy")
    result = _proxy_db().export_month(year, month, output_dir)

    # Trigger dashboard data reload
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    return jsonify(result)


# ── WebDAV Sync ──────────────────────────────────────────────────────

@bp_proxy.route("/sync", methods=["POST"])
def sync_database():
    from app.sync import sync as do_sync

    db_path = current_app.config["PROXY_DB"].db_path
    result = do_sync(db_path)

    # Reload dashboard data if sync brought new records
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    status_code = 200 if result["status"] == "ok" else 400
    return jsonify(result), status_code


@bp_proxy.route("/sync/config", methods=["GET"])
def get_sync_config():
    from app.sync import load_sync_config

    db_path = current_app.config["PROXY_DB"].db_path
    cfg = load_sync_config(db_path)
    if cfg:
        return jsonify({
            "url": cfg.url,
            "username": cfg.username,
            "password": "••••••" if cfg.password else "",
            "has_password": bool(cfg.password),
        })
    return jsonify({"url": "", "username": "", "password": "", "has_password": False})


@bp_proxy.route("/sync/config", methods=["PUT"])
def save_sync_config():
    from app.sync import SyncConfig, save_sync_config as save_cfg

    data = request.get_json(force=True)
    if not data.get("url") or not data.get("username"):
        return jsonify({"error": "url and username are required"}), 400

    db_path = current_app.config["PROXY_DB"].db_path

    # If password is masked placeholder, preserve the existing one
    if data.get("password", "").startswith("••••"):
        from app.sync import load_sync_config
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""
    else:
        password = data.get("password", "")

    cfg = SyncConfig(url=data["url"], username=data["username"], password=password)
    save_cfg(db_path, cfg)
    return jsonify({"status": "ok"})


@bp_proxy.route("/sync/test", methods=["POST"])
def test_sync_connection():
    from app.sync import SyncConfig, _webdav_test

    data = request.get_json(force=True)
    db_path = current_app.config["PROXY_DB"].db_path

    # Build config from request (or fall back to saved config)
    url = data.get("url")
    username = data.get("username")
    password = data.get("password", "")

    if password.startswith("••••"):
        from app.sync import load_sync_config
        existing = load_sync_config(db_path)
        password = existing.password if existing else ""

    if not url or not username:
        return jsonify({"error": "url and username are required"}), 400

    cfg = SyncConfig(url=url, username=username, password=password)
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
