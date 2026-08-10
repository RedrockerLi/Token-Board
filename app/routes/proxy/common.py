"""Flask Blueprint for proxy management API endpoints.

Mounts at /api/proxy/ — provides CRUD for accounts, keys, pricing,
and read-only billing/usage endpoints backed by the SQLite database
shared with the C++ proxy.
"""

from flask import Blueprint, current_app, jsonify, request

bp_proxy = Blueprint("proxy", __name__, url_prefix="/api/proxy")


def _proxy_db():
    """Return the ProxyDatabase instance from app config."""
    return current_app.config["PROXY_DB"]



__all__ = [name for name in globals() if not name.startswith('__')]
