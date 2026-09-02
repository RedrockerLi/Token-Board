"""CRUD endpoints for independent agent subscriptions and software."""

from app.routes.proxy.common import (
    _proxy_db, api_error, bp_proxy, jsonify, require_json_object,
    require_config_writable,
)


@bp_proxy.route("/agent-types")
def agent_types():
    return jsonify(_proxy_db().get_agent_types())


@bp_proxy.route("/agent-subscriptions", methods=["GET"])
def list_agent_subscriptions():
    return jsonify(_proxy_db().get_agent_subscriptions())


@bp_proxy.route("/agent-subscriptions", methods=["POST"])
@require_config_writable
def create_agent_subscription():
    try:
        return jsonify({"id": _proxy_db().create_agent_subscription(
            require_json_object(force=True))}), 201
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>", methods=["PUT"])
@require_config_writable
def update_agent_subscription(subscription_id):
    try:
        if not _proxy_db().update_agent_subscription(
                subscription_id, require_json_object(force=True)):
            return api_error("Subscription not found", 404)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>", methods=["DELETE"])
@require_config_writable
def delete_agent_subscription(subscription_id):
    if not _proxy_db().delete_agent_subscription(subscription_id):
        return api_error("Subscription not found", 404)
    return jsonify({"status": "ok"})


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>/instances", methods=["GET"])
def list_agent_subscription_instances(subscription_id):
    return jsonify(_proxy_db().get_agent_subscription_instances(subscription_id))


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>/instances", methods=["POST"])
@require_config_writable
def create_agent_subscription_instance(subscription_id):
    try:
        return jsonify({"id": _proxy_db().create_agent_subscription_instance(
            subscription_id, require_json_object(force=True))}), 201
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-subscription-instances/<int:instance_id>", methods=["PUT"])
@require_config_writable
def update_agent_subscription_instance(instance_id):
    try:
        if not _proxy_db().update_agent_subscription_instance(
                instance_id, require_json_object(force=True)):
            return api_error("Instance not found", 404)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-subscription-instances/<int:instance_id>", methods=["DELETE"])
@require_config_writable
def delete_agent_subscription_instance(instance_id):
    if not _proxy_db().delete_agent_subscription_instance(instance_id):
        return api_error("Instance not found", 404)
    return jsonify({"status": "ok"})


@bp_proxy.route("/agent-software", methods=["GET"])
def list_agent_software():
    return jsonify(_proxy_db().get_agent_software())


@bp_proxy.route("/agent-software", methods=["POST"])
@require_config_writable
def create_agent_software():
    try:
        return jsonify({"id": _proxy_db().create_agent_software(
            require_json_object(force=True))}), 201
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-software/<int:software_id>", methods=["PUT"])
@require_config_writable
def update_agent_software(software_id):
    try:
        if not _proxy_db().update_agent_software(
                software_id, require_json_object(force=True)):
            return api_error("Software not found", 404)
        return jsonify({"status": "ok"})
    except Exception as exc:
        return api_error(str(exc), 400)


@bp_proxy.route("/agent-software/<int:software_id>", methods=["DELETE"])
@require_config_writable
def delete_agent_software(software_id):
    if not _proxy_db().delete_agent_software(software_id):
        return api_error("Software not found", 404)
    return jsonify({"status": "ok"})
