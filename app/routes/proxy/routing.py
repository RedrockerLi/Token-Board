"""Functional proxy API route group."""

from app.routes.proxy.common import (
    _proxy_db, api_error, bp_proxy, jsonify, request, require_json_object,
)
from app.routes.contract import status_for

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
    data = require_json_object(force=True)
    if not data.get("account_id"):
        return api_error("account_id is required", 400)
    try:
        key_value = _proxy_db().create_key(data)
        return jsonify({"key_value": key_value}), 201
    except Exception as e:
        return api_error(str(e), 400)


@bp_proxy.route("/keys/<int:key_id>", methods=["PUT"])
def update_key(key_id):
    data = require_json_object(force=True)
    ok = _proxy_db().update_key(key_id, data)
    if not ok:
        return api_error("No fields to update or key not found", 400)
    return jsonify({"status": "ok"})


@bp_proxy.route("/keys/<int:key_id>", methods=["DELETE"])
def delete_key(key_id):
    try:
        ok = _proxy_db().delete_key(key_id)
    except ValueError as e:
        return api_error(
            str(e), status_for("key_delete", {"status": "error"}))
    if not ok:
        return api_error(
            "Key not found", status_for("key_delete", {"status": "not_found"}))
    return jsonify({"status": "ok"})


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
    data = require_json_object(force=True)
    if not data.get("name"):
        return api_error("name is required", 400)
    err = _validate_aggregate_entries(data.get("entries"))
    if err:
        return api_error(err, 400)
    try:
        agg_id = _proxy_db().create_aggregate(data)
        return jsonify({"id": agg_id}), 201
    except Exception as e:
        return api_error(str(e), 400)


@bp_proxy.route("/aggregates/<int:agg_id>", methods=["PUT"])
def update_aggregate(agg_id):
    data = require_json_object(force=True)
    err = _validate_aggregate_entries(data.get("entries"))
    if err:
        return api_error(err, 400)
    ok = _proxy_db().update_aggregate(agg_id, data)
    if not ok:
        return api_error("Aggregate account not found", 404)
    return jsonify({"status": "ok"})


@bp_proxy.route("/aggregates/<int:agg_id>", methods=["DELETE"])
def delete_aggregate(agg_id):
    ok = _proxy_db().delete_aggregate(agg_id)
    if not ok:
        return api_error("Aggregate account not found", 404)
    return jsonify({"status": "ok"})
