"""Flask Blueprint: all page routes and API endpoints."""

from collections import defaultdict

from flask import Blueprint, current_app, jsonify, render_template, request

bp = Blueprint("dashboard", __name__)


def _store():
    """Shortcut to the DataStore singleton stored in Flask app config."""
    return current_app.config["DATA_STORE"]


# ── Page Routes ────────────────────────────────────────────────────────


__all__ = [name for name in globals() if not name.startswith('__')]
