"""Agent usage import wake controls for token-maintenance."""

from app.routes.proxy.common import bp_proxy, current_app, jsonify


@bp_proxy.route("/agent-usage/import", methods=["POST"])
def trigger_agent_usage_import():
    """Wake the single maintenance-owned importer without blocking the request."""
    from app.services.runtime_tasks import trigger_agent_usage_import as trigger

    if not trigger(current_app):
        return jsonify({
            "status": "unavailable",
            "message": "agent usage importer is not running",
        }), 503
    return jsonify({"status": "scheduled"}), 202
