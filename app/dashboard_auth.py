"""Lightweight dashboard access-token auth.

Active only when the dashboard is reachable off-loopback (--host 0.0.0.0 / a
LAN/remote address) OR TB_DASHBOARD_TOKEN is set.  Loopback binding stays
password-free for local convenience.  When the app is exposed to the network
with no configured token, a random token is minted, persisted to
`data/dashboard_token.txt` (so restarts don't invalidate saved cookies) and
printed at startup.

Every request is checked (cookie `tb_dash_token` or `X-Dashboard-Token`
header); `/login`, `/logout` and `/static/*` are exempt.  API/XHR requests get
a 401 JSON, browsers get redirected to /login.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, redirect, request, url_for

COOKIE_NAME = "tb_dash_token"


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def resolve_token(host: str, data_dir: Path) -> str | None:
    """Return the access token, or None to keep auth disabled (loopback)."""
    env = os.environ.get("TB_DASHBOARD_TOKEN", "").strip()
    if env:
        return env
    if _is_loopback(host):
        return None
    token_file = data_dir / "dashboard_token.txt"
    if token_file.exists():
        existing = token_file.read_text().strip()
        if existing:
            return existing
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    token_file.write_text(token)
    return token


def _unauthorized() -> Response:
    if request.path.startswith("/api/") or (
        request.accept_mimetypes.best == "application/json"
    ):
        return jsonify({"error": "unauthorized",
                        "message": "Dashboard access token required"} ), 401
    return redirect(url_for("dashboard_auth.login"))


def install_auth(app: Flask, token: str) -> None:
    """Register token enforcement + /login /logout on an existing app."""

    @app.before_request
    def _guard() -> Response | None:
        if request.path in ("/login", "/logout") or request.path.startswith("/static/"):
            return None
        provided = request.cookies.get(COOKIE_NAME) or request.headers.get("X-Dashboard-Token")
        if provided and secrets.compare_digest(provided, token):
            return None
        return _unauthorized()

    @app.route("/login", methods=["GET", "POST"], endpoint="dashboard_auth.login")
    def login():
        if request.method == "POST":
            value = (request.form.get("token") or "").strip()
            if value and secrets.compare_digest(value, token):
                resp = make_response(redirect(request.args.get("next") or "/"))
                resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax",
                                max_age=30 * 24 * 3600)
                return resp
        return (
            "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
            "<title>登录 — Token Board</title>"
            "<style>body{font-family:system-ui;display:flex;align-items:center;"
            "justify-content:center;min-height:100vh;background:#111;color:#eee}"
            "form{background:#1c1c1c;padding:32px;border-radius:12px;width:300px}"
            "input{width:100%;box-sizing:border-box;padding:10px;margin:10px 0;"
            "border-radius:6px;border:1px solid #444;background:#222;color:#eee}"
            "button{width:100%;padding:10px;border:0;border-radius:6px;"
            "background:#3b82f6;color:#fff;cursor:pointer}</style>"
            "</head><body><form method='post'>"
            "<h2 style='margin:0'>Token Board 登录</h2>"
            "<input type='password' name='token' placeholder='访问口令' autofocus>"
            "<button type='submit'>登录</button></form></body></html>"
        )

    @app.route("/logout", methods=["POST"], endpoint="dashboard_auth.logout")
    def logout():
        resp = make_response(redirect("/login"))
        resp.delete_cookie(COOKIE_NAME)
        return resp
