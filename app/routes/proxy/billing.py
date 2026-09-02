"""Functional proxy API route group."""

from app.routes.proxy.common import (
    _proxy_db, api_error, bp_proxy, current_app, jsonify, request,
    require_json_object, require_config_writable,
)


# Client wire-format groups accepted by the timeout configuration API.
_TIMEOUT_GROUPS = ("anthropic", "openai_responses", "openai")


@bp_proxy.route("/pricing", methods=["GET"])
def list_pricing():
    return jsonify(_proxy_db().get_pricing())


@bp_proxy.route("/pricing", methods=["POST"])
@require_config_writable
def create_pricing():
    data = require_json_object(force=True)
    if not data.get("model_pattern"):
        return api_error("model_pattern is required", 400)
    try:
        pid = _proxy_db().create_pricing(data)
        return jsonify({"id": pid}), 201
    except Exception as e:
        return api_error(str(e), 400)


@bp_proxy.route("/pricing/<int:pricing_id>", methods=["PUT"])
@require_config_writable
def update_pricing(pricing_id):
    data = require_json_object(force=True)
    try:
        ok = _proxy_db().update_pricing(pricing_id, data)
    except (TypeError, ValueError) as e:
        return api_error(str(e), 400)
    if not ok:
        return api_error("No fields to update or pricing not found", 400)
    return jsonify({"status": "ok"})


@bp_proxy.route("/pricing/<int:pricing_id>", methods=["DELETE"])
@require_config_writable
def delete_pricing(pricing_id):
    ok = _proxy_db().delete_pricing(pricing_id)
    if not ok:
        return api_error("Pricing entry not found", 404)
    return jsonify({"status": "ok"})


@bp_proxy.route("/pricing/reorder", methods=["POST"])
@require_config_writable
def reorder_pricing():
    data = require_json_object(force=True)
    try:
        ok = _proxy_db().reorder_pricing_order(data["ids"])
    except KeyError:
        return api_error("ids is required", 400)
    except (TypeError, ValueError) as e:
        return api_error(str(e), 400)
    if not ok:
        return api_error("Pricing entry not found", 404)
    return jsonify({"status": "ok"})


@bp_proxy.route("/timeout-config", methods=["GET"])
def get_timeout_config():
    cfg = _proxy_db().get_timeout_config()
    return jsonify({row["app_type"]: row for row in cfg})


@bp_proxy.route("/timeout-config", methods=["PUT"])
@require_config_writable
def save_timeout_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return api_error("请求体必须包含各分组配置", 400)
    groups = {g: data[g] for g in _TIMEOUT_GROUPS
              if isinstance(data.get(g), dict)}
    if not groups:
        return api_error("缺少有效的分组配置", 400)
    try:
        for app_type, group in groups.items():
            _proxy_db().update_timeout_config(app_type, group)
    except ValueError as e:
        return api_error(str(e), 400)
    return jsonify({"status": "ok"})


@bp_proxy.route("/billing-config", methods=["GET"])
def get_billing_config():
    return jsonify(_proxy_db().get_plan_billing_config())


@bp_proxy.route("/billing-config", methods=["PUT"])
@require_config_writable
def save_billing_config():
    try:
        _proxy_db().update_plan_billing_config(require_json_object(force=True))
    except ValueError as e:
        return api_error(str(e), 400)
    return jsonify(_proxy_db().get_plan_billing_config())


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
