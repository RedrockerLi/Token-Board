"""Dashboard route group."""

from app.routes.dashboard.common import bp, jsonify, render_template, _store

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
