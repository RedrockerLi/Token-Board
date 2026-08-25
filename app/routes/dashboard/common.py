"""Flask Blueprint: all page routes and API endpoints."""

from collections import defaultdict

from flask import Blueprint, current_app, jsonify, render_template, request

bp = Blueprint("dashboard", __name__)


def _store():
    """Shortcut to the DataStore singleton stored in Flask app config."""
    return current_app.config["DATA_STORE"]


def api_error(message: str, status: int):
    """Render an explicit dashboard JSON error at the route boundary."""

    return jsonify({"error": str(message)}), int(status)


# ── Page Routes ────────────────────────────────────────────────────────


__all__ = ["bp", "current_app", "jsonify", "render_template", "request",
           "_store", "api_error"]
