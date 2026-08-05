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
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 400
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
    # mode: "detach" (default) = unbind keys (account_id → NULL); "cascade" = delete keys too.
    mode = request.args.get("mode", "detach")
    if mode not in ("detach", "cascade"):
        return jsonify({"error": "mode must be 'detach' or 'cascade'"}), 400
    result = _proxy_db().delete_account(account_id, mode=mode)
    if not result["ok"]:
        return jsonify({"error": result["error"] or "Account not found"}), 400
    return jsonify({"status": "ok", "cancelled_at": result.get("cancelled_at"),
                    "cancellation_grace_hours": result.get("cancellation_grace_hours"),
                    "cancellation_effects": result.get("cancellation_effects", [])})


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


@bp_proxy.route("/accounts/<int:account_id>/test-concurrency", methods=["POST"])
def test_account_concurrency(account_id):
    """按账户并发限额，直连上游并行发 N 个极小聊天请求，检验该并发是否安全。

    不经本机 C++ 代理：直接用该账户的 upstream_key 并发打上游，观察在该
    并发下上游是否会报错（429 / 5xx / 超时）。自动挑选模型定价列表中最便宜
    的、且该账户可用的模型，尽量压低测试成本。
    """
    import fnmatch
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    import requests

    db = _proxy_db()
    acc = next((a for a in db.get_accounts() if a["id"] == account_id), None)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    if acc.get("is_aggregate"):
        return jsonify({"error": "聚合账户不支持并发测试"}), 400
    key = acc.get("upstream_key") or ""
    if not key:
        return jsonify({"error": "该账户未配置上游 Key，请先在账户编辑中填写 Key"}), 400

    # 并发数：请求体可覆盖（弹窗里测未保存的值），否则用已保存的限额。
    data = request.get_json(silent=True) or {}
    concurrency = data.get("concurrency") if data.get("concurrency") is not None else acc.get("max_concurrency")
    try:
        concurrency = int(concurrency)
    except (TypeError, ValueError):
        return jsonify({"error": "缺少并发数：该账户未设置并发限额，无法测试"}), 400
    concurrency = max(1, min(concurrency, 50))

    # ── 选最便宜的模型：定价 (input+output 总价) 升序，GLOB 匹配该账户可用模型 ──
    models = db.get_account_models(account_id)
    if not models:  # 复用 update_account_models 的实时拉取模式
        try:
            resp = requests.get(
                acc["base_url"].rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + key},
                timeout=15,
            )
            if resp.ok:
                models = [m["id"] for m in resp.json().get("data", [])]
                db.update_account_models(account_id, models)
        except Exception:
            pass
    if not models:
        return jsonify({"error": "该账户暂无模型，请先点击「更新模型」获取模型列表"}), 400

    pricing = sorted(
        db.get_pricing(),
        key=lambda p: (p.get("input_price") or 0) + (p.get("output_price") or 0),
    )
    model_id = next(
        (m for p in pricing for m in models
         if fnmatch.fnmatch(m.lower(), p["model_pattern"].lower())),
        None,
    )
    if not model_id:
        return jsonify({"error": f"没有匹配的定价条目（账户模型如: {models[0]}），请先在「模型定价」配置匹配模式"}), 400

    # ── 按账户格式拼 URL / body / headers（与 C++ resolve_upstream_target 对齐）──
    fmt = acc.get("api_format") or "openai"
    base = (acc.get("base_url") or "").rstrip("/")
    ep = (acc.get("endpoint_path") or "").strip()

    def _url():
        if ep.startswith(("http://", "https://")):
            return ep
        scheme = base.split("://", 1)[0] + "://" if "://" in base else ""
        host = base.split("://", 1)[1].split("/", 1)[0] if "://" in base else base
        if ep:
            return scheme + host + "/" + ep.lstrip("/")
        if fmt == "anthropic":
            return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        if fmt == "openai_responses":
            return base + "/responses"
        return base + "/chat/completions"

    def _body():
        if fmt == "anthropic":
            return {"model": model_id, "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}]}
        if fmt == "openai_responses":
            return {"model": model_id, "input": "ping", "max_output_tokens": 8}
        return {"model": model_id, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8, "stream": False}

    scheme = acc.get("auth_header") or "auto"
    if scheme == "auto":
        scheme = "x-api-key" if fmt == "anthropic" else "bearer"
    headers = {"Content-Type": "application/json"}
    if scheme == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + key

    url, body = _url(), _body()

    def _one(_i):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=(10, 30))
            if 200 <= resp.status_code < 300:
                return (True, None)
            return (False, f"HTTP {resp.status_code}")
        except Exception as e:
            return (False, f"{type(e).__name__}: {str(e)[:120]}")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        results = [f.result() for f in (ex.submit(_one, i) for i in range(concurrency))]

    succeeded = sum(1 for ok, _ in results if ok)
    failures = [detail for ok, detail in results if not ok]
    failed = len(failures)

    if failed == 0:
        message = f"并发 {concurrency} · 模型 {model_id} · {succeeded}/{concurrency} 成功，该并发安全"
    else:
        detail = "；".join(f"{n} 个失败({k})" for k, n in Counter(failures).most_common(3))
        if all(d.startswith("HTTP 4") for d in failures):
            message = (f"并发 {concurrency} · 模型 {model_id} · {succeeded}/{concurrency} 成功，"
                       f"{failed} 个失败（{detail}）——全部为 4xx，多为模型/Key/鉴权问题，请检查配置")
        else:
            message = (f"并发 {concurrency} · 模型 {model_id} · {succeeded}/{concurrency} 成功，"
                       f"{failed} 个失败（{detail}），建议降低并发或检查上游")

    return jsonify({"status": "ok", "concurrency": concurrency, "model": model_id,
                    "succeeded": succeeded, "failed": failed,
                    "failures": failures[:10], "message": message})


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


# ── Aggregate Accounts ────────────────────────────────────────────────

def _validate_aggregate_entries(entries):
    """Each entry is a concrete model name (no wildcards) → the aggregate's
    catalog, plus the real upstream account + model it routes to."""
    if not isinstance(entries, list) or not entries:
        return "至少需要一条模型映射"
    for e in entries:
        if not e.get("pattern") or not e.get("account_id") or not e.get("upstream_model"):
            return "每条映射需要填写模型名称、目标账户和目标模型"
        if any(ch in e["pattern"] for ch in "*?"):
            return f"模型名称 '{e['pattern']}' 不能包含通配符（* ?）——聚合账户的模型列表为精确模型名"
    return None


@bp_proxy.route("/aggregates", methods=["GET"])
def list_aggregates():
    return jsonify(_proxy_db().get_aggregates())


@bp_proxy.route("/aggregates", methods=["POST"])
def create_aggregate():
    data = request.get_json(force=True)
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 400
    err = _validate_aggregate_entries(data.get("entries"))
    if err:
        return jsonify({"error": err}), 400
    try:
        agg_id = _proxy_db().create_aggregate(data)
        return jsonify({"id": agg_id}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/aggregates/<int:agg_id>", methods=["PUT"])
def update_aggregate(agg_id):
    data = request.get_json(force=True)
    err = _validate_aggregate_entries(data.get("entries"))
    if err:
        return jsonify({"error": err}), 400
    ok = _proxy_db().update_aggregate(agg_id, data)
    if not ok:
        return jsonify({"error": "Aggregate account not found"}), 404
    return jsonify({"status": "ok"})


@bp_proxy.route("/aggregates/<int:agg_id>", methods=["DELETE"])
def delete_aggregate(agg_id):
    ok = _proxy_db().delete_aggregate(agg_id)
    if not ok:
        return jsonify({"error": "Aggregate account not found"}), 404
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


# ── Timeout config (per client wire format) ───────────────────────────

_TIMEOUT_GROUPS = ("anthropic", "openai_responses", "openai")


@bp_proxy.route("/timeout-config", methods=["GET"])
def get_timeout_config():
    cfg = _proxy_db().get_timeout_config()
    return jsonify({row["app_type"]: row for row in cfg})


@bp_proxy.route("/timeout-config", methods=["PUT"])
def save_timeout_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须包含各分组配置"}), 400
    groups = {g: data[g] for g in _TIMEOUT_GROUPS
              if isinstance(data.get(g), dict)}
    if not groups:
        return jsonify({"error": "缺少有效的分组配置"}), 400
    try:
        for app_type, group in groups.items():
            _proxy_db().update_timeout_config(app_type, group)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok"})


# ── Plan billing settings ──────────────────────────────────────────────

@bp_proxy.route("/billing-config", methods=["GET"])
def get_billing_config():
    return jsonify(_proxy_db().get_plan_billing_config())


@bp_proxy.route("/billing-config", methods=["PUT"])
def save_billing_config():
    try:
        _proxy_db().update_plan_billing_config(request.get_json(force=True))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_proxy_db().get_plan_billing_config())


# ── Billing / Usage ────────────────────────────────────────────────────

@bp_proxy.route("/billing")
def billing_summary():
    account_id = request.args.get("account_id", type=int)
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    days = request.args.get("days", type=int, default=30)
    return jsonify(
        _proxy_db().get_billing_summary(account_id, date_from, date_to, days=days)
    )


@bp_proxy.route("/billing/daily")
def daily_billing():
    days = request.args.get("days", type=int, default=30)
    return jsonify(_proxy_db().get_daily_billing(days))


@bp_proxy.route("/billing/daily-by-model")
def daily_billing_by_model():
    days = request.args.get("days", type=int, default=30)
    return jsonify(_proxy_db().get_daily_billing_by_model(days))


@bp_proxy.route("/billing/recent-days")
def recent_billing_days():
    days = request.args.get("days", type=int, default=30)
    return jsonify(_proxy_db().get_recent_billing_days(days))


@bp_proxy.route("/billing/today-upstreams")
def billing_today_upstreams():
    return jsonify(_proxy_db().get_today_upstream_usage())


@bp_proxy.route("/export", methods=["POST"])
def export_data():
    """Export unexported request_log → dashboard.db, then sync to cloud.

    Full pipeline: pull remote dashboard → export local → push back.
    """
    import os as _os
    from app.sync import sync_dashboard

    db_path = current_app.config["PROXY_DB"].db_path
    dash_db_path = _os.path.join(_os.path.dirname(db_path), "dashboard.db")

    result = sync_dashboard(db_path, dash_db_path)

    # Trigger dashboard data reload
    ds = current_app.config.get("DATA_STORE")
    if ds:
        ds.load()

    return jsonify(result)



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


@bp_proxy.route("/sync/config/upload", methods=["POST"])
def upload_config():
    """Upload local config to cloud as one transaction (exit settings page).

    Never uploads upstream API keys or the WebDAV credentials. Refuses
    (conflict) if the cloud moved past this machine's last sync.
    """
    from app.sync import sync_config_upload

    db_path = current_app.config["PROXY_DB"].db_path
    result = sync_config_upload(db_path)
    return jsonify(result)


@bp_proxy.route("/sync/config/discard", methods=["POST"])
def discard_config():
    """Roll local config tables back to the last-committed snapshot.

    Called when the user chooses "丢弃设置" after a failed upload — local
    edits are reverted (including per-machine upstream keys) without network.
    """
    from app.sync import restore_config_snapshot

    db_path = current_app.config["PROXY_DB"].db_path
    if not restore_config_snapshot(db_path):
        return jsonify({"status": "error",
                        "message": "没有可回滚的快照(本机尚未成功同步过)"}), 400
    return jsonify({"status": "ok", "message": "已回滚到上次同步状态"})


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
            include_attempts=request.args.get("include_attempts", "1").lower()
            in {"1", "true", "yes"},
        )
    )


# ── Performance Metrics ────────────────────────────────────────────────

@bp_proxy.route("/perf/summary")
def perf_summary():
    minutes = request.args.get("minutes", 15, type=int)
    return jsonify(_proxy_db().get_perf_summary(minutes))


@bp_proxy.route("/perf/upstream-success-rate")
def perf_upstream_success_rate():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_upstream_success_rate(minutes))


@bp_proxy.route("/perf/latency")
def perf_latency():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_latency(minutes))


@bp_proxy.route("/perf/speed")
def perf_speed():
    minutes = request.args.get("minutes", 60, type=int)
    return jsonify(_proxy_db().get_perf_speed(minutes))


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
