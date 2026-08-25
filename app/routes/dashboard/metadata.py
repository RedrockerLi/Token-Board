"""Dashboard route group."""

from app.routes.dashboard.common import bp, current_app, jsonify, render_template, _store

@bp.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


@bp.route("/api/refresh")
def api_refresh():
    """Rebuild the in-memory store from the dashboard archive."""
    _store().load()
    return jsonify({
        "status": "ok",
        "months": len(_store().available_months),
        "token_records": len(_store().token_usages),
        "request_records": len(_store().request_usages),
        "cost_records": len(_store().cost_entries),
    })


@bp.route("/api/api_key_names")
def api_api_key_names():
    """Return sorted list of unique api_key_name values."""
    return jsonify(_store().api_key_names)


@bp.route("/api/models")
def api_models():
    """Return models grouped by platform.

    Returns:
        dict: {model_name: {"platform": platform_name, ...}}
    """
    model_map: dict[str, dict] = {}
    for tu in _store().token_usages:
        if tu["model"] not in model_map:
            model_map[tu["model"]] = {"platform": tu["platform"]}
    for ru in _store().request_usages:
        if ru["model"] not in model_map:
            model_map[ru["model"]] = {"platform": ru["platform"]}
    for ce in _store().cost_entries:
        if ce["model"] not in model_map:
            model_map[ce["model"]] = {"platform": ce["platform"]}
    return jsonify(model_map)
