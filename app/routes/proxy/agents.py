"""CRUD endpoints for independent agent subscriptions and software."""

from app.routes.proxy.common import *  # noqa: F401,F403


@bp_proxy.route("/agent-types")
def agent_types():
    return jsonify(_proxy_db().get_agent_types())


@bp_proxy.route("/agent-subscriptions", methods=["GET"])
def list_agent_subscriptions():
    return jsonify(_proxy_db().get_agent_subscriptions())


@bp_proxy.route("/agent-subscriptions", methods=["POST"])
def create_agent_subscription():
    try:
        return jsonify({"id": _proxy_db().create_agent_subscription(
            request.get_json(force=True))}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>", methods=["PUT"])
def update_agent_subscription(subscription_id):
    try:
        if not _proxy_db().update_agent_subscription(
                subscription_id, request.get_json(force=True)):
            return jsonify({"error": "Subscription not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>", methods=["DELETE"])
def delete_agent_subscription(subscription_id):
    if not _proxy_db().delete_agent_subscription(subscription_id):
        return jsonify({"error": "Subscription not found"}), 404
    return jsonify({"status": "ok"})


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>/instances", methods=["GET"])
def list_agent_subscription_instances(subscription_id):
    return jsonify(_proxy_db().get_agent_subscription_instances(subscription_id))


@bp_proxy.route("/agent-subscriptions/<int:subscription_id>/instances", methods=["POST"])
def create_agent_subscription_instance(subscription_id):
    try:
        return jsonify({"id": _proxy_db().create_agent_subscription_instance(
            subscription_id, request.get_json(force=True))}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-subscription-instances/<int:instance_id>", methods=["PUT"])
def update_agent_subscription_instance(instance_id):
    try:
        if not _proxy_db().update_agent_subscription_instance(
                instance_id, request.get_json(force=True)):
            return jsonify({"error": "Instance not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-subscription-instances/<int:instance_id>", methods=["DELETE"])
def delete_agent_subscription_instance(instance_id):
    if not _proxy_db().delete_agent_subscription_instance(instance_id):
        return jsonify({"error": "Instance not found"}), 404
    return jsonify({"status": "ok"})


@bp_proxy.route("/agent-software", methods=["GET"])
def list_agent_software():
    return jsonify(_proxy_db().get_agent_software())


@bp_proxy.route("/agent-software", methods=["POST"])
def create_agent_software():
    try:
        return jsonify({"id": _proxy_db().create_agent_software(
            request.get_json(force=True))}), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-software/<int:software_id>", methods=["PUT"])
def update_agent_software(software_id):
    try:
        if not _proxy_db().update_agent_software(
                software_id, request.get_json(force=True)):
            return jsonify({"error": "Software not found"}), 404
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@bp_proxy.route("/agent-software/<int:software_id>", methods=["DELETE"])
def delete_agent_software(software_id):
    if not _proxy_db().delete_agent_software(software_id):
        return jsonify({"error": "Software not found"}), 404
    return jsonify({"status": "ok"})
