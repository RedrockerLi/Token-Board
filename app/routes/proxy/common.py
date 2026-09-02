"""Flask Blueprint for proxy management API endpoints.

Mounts at /api/proxy/ — provides CRUD for accounts, keys, pricing,
and read-only billing/usage endpoints backed by the SQLite database
shared with the C++ proxy.
"""

from functools import wraps

from flask import Blueprint, current_app, jsonify, request

bp_proxy = Blueprint("proxy", __name__, url_prefix="/api/proxy")


def _proxy_db():
    """Return the ProxyDatabase instance from app config."""
    return current_app.config["TOKEN_BOARD_DB"]


def api_error(message: str, status: int):
    """Render one explicit JSON error without changing exception semantics."""

    return jsonify({"error": str(message)}), int(status)


def require_json_object(*, force: bool = False, silent: bool = False) -> dict:
    """Validate an object request body at a route boundary.

    The caller decides whether malformed JSON should retain Flask's historical
    ``force=True`` behavior; this helper does not install a global handler.
    """

    data = request.get_json(force=force, silent=silent)
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def config_session():
    return current_app.config.get("CONFIG_SESSION")


def require_config_writable(view):
    """Reject synchronized-config mutations until the cloud baseline is ready."""
    @wraps(view)
    def guarded(*args, **kwargs):
        session = config_session()
        if session is not None and not session.is_writable():
            status = session.status()
            return jsonify({
                "status": "read_only",
                "state": status.state,
                "message": status.message or "云端配置尚未就绪，当前设置为只读",
            }), 423
        return view(*args, **kwargs)
    return guarded



__all__ = ["bp_proxy", "current_app", "jsonify", "request", "_proxy_db",
           "api_error", "require_json_object", "config_session",
           "require_config_writable"]
