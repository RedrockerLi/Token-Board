"""Functional proxy API route group."""

from app.routes.proxy.common import *  # noqa: F401,F403

@bp_proxy.route("/stats")
def stats():
    return jsonify(_proxy_db().get_stats())


@bp_proxy.route("/account-types")
def account_types():
    """The upstream account-type spec table — frontend's single source of
    truth for type behavior (routable / subscription / holds_keys / …)."""
    from app.domain.account_types import as_payload

    return jsonify(as_payload())


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
    return jsonify({"status": "ok",
                    "cancellation_mode": result.get("cancellation_mode"),
                    "cancelled_at": result.get("cancelled_at"),
                    "effective_deleted_at": result.get("effective_deleted_at"),
                    "deferred": result.get("deferred", False)})


@bp_proxy.route("/accounts/<int:account_id>/cloud-keys", methods=["POST"])
def confirm_cloud_key(account_id):
    """补填 cloud-only 密钥的明文：云端镜像里有、本机没有 → 写入 upstream_keys。

    body: ``{"masked": "sk-abc…wxyz", "key_value": "sk-...真实明文"}``
    """
    data = request.get_json(force=True)
    try:
        ok = _proxy_db().confirm_cloud_key(
            account_id,
            (data.get("masked") or "").strip(),
            (data.get("key_value") or "").strip(),
        )
        if not ok:
            return jsonify({"error": "云端没有该密钥记录"}), 404
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/accounts/<int:account_id>/models", methods=["POST"])
def update_account_models(account_id):
    """Fetch models from upstream and store them for this account."""
    db = _proxy_db()
    acc = next((a for a in db.get_accounts() if a["id"] == account_id), None)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    keys = db.get_plain_keys(account_id)
    key = keys[0] if keys else ""

    try:
        from app.services.upstream_probe import model_probe
        resp = model_probe(acc, key)
        if not resp.ok:
            return jsonify({"error": f"Upstream HTTP {resp.status_code}: {resp.text[:200]}"}), 400
        data = resp.json()
        models = [m["id"] for m in data.get("data", [])]
        count = db.update_account_models(account_id, models)
        return jsonify({"status": "ok", "count": count, "models": models})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp_proxy.route("/accounts/<int:account_id>/models", methods=["GET"])
def get_account_models(account_id):
    return jsonify(_proxy_db().get_account_models(account_id))


@bp_proxy.route("/accounts/<int:account_id>/test-concurrency", methods=["POST"])
def test_account_concurrency(account_id):
    """按账户并发限额，直连上游并行发 N 个极小聊天请求，检验该并发是否安全。

    不经本机 C++ 代理：直接用该账户的第一把上游 Key 并发打上游，观察在该
    并发下上游是否会报错（429 / 5xx / 超时）。自动挑选模型定价列表中最便宜
    的、且该账户可用的模型，尽量压低测试成本。
    """
    import fnmatch
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    db = _proxy_db()
    acc = next((a for a in db.get_accounts() if a["id"] == account_id), None)
    if not acc:
        return jsonify({"error": "Account not found"}), 404
    if acc.get("is_aggregate"):
        return jsonify({"error": "聚合账户不支持并发测试"}), 400
    from app.domain.account_types import spec as _type_spec
    type_spec = _type_spec(acc.get("account_type") or "api")
    if not type_spec.holds_keys:
        return jsonify({"error": "该账户类型不持有上游密钥，不支持并发测试"}), 400
    keys = db.get_plain_keys(account_id)
    key = keys[0] if keys else ""
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
            from app.services.upstream_probe import model_probe
            resp = model_probe(acc, key)
            if resp.ok:
                models = [m["id"] for m in resp.json().get("data", [])]
                db.update_account_models(account_id, models)
        except Exception as exc:
            current_app.logger.warning(
                "live model discovery failed for account %s: %s",
                account_id, exc,
            )
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

    # ── 按账户格式生成请求体；URL/auth 由共享 probe helper 解析 ──
    fmt = acc.get("api_format") or "openai"

    def _body():
        if fmt == "anthropic":
            return {"model": model_id, "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}]}
        if fmt == "openai_responses":
            return {"model": model_id, "input": "ping", "max_output_tokens": 8}
        return {"model": model_id, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8, "stream": False}

    from app.services.upstream_probe import request_probe
    body = _body()

    def _one(_i):
        try:
            resp = request_probe(acc, key, body)
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
