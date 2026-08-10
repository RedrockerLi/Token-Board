"""Shared management-plane upstream URL and authentication helpers.

The C++ proxy owns request forwarding.  These helpers are only for dashboard
probes (model discovery and the concurrency check), but they deliberately use
the same endpoint/auth normalization as the runtime so a configured Anthropic
or custom-path upstream is not accidentally probed with Bearer credentials.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _origin(base_url: str) -> str:
    parts = urlsplit((base_url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError("上游 Base URL 必须包含协议和主机名")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def _base_path(base_url: str) -> str:
    return urlsplit((base_url or "").strip()).path.rstrip("/")


def endpoint_url(account: dict, endpoint: str) -> str:
    """Resolve a management endpoint using account endpoint semantics."""
    base = str(account.get("base_url") or "").rstrip("/")
    configured = str(account.get("endpoint_path") or "").strip()
    if endpoint not in {"models", "chat", "messages", "responses"}:
        raise ValueError(f"unsupported probe endpoint: {endpoint}")
    if configured:
        if configured.startswith(("http://", "https://")):
            return configured
        return _origin(base) + "/" + configured.lstrip("/")

    fmt = str(account.get("api_format") or "openai").strip().lower()
    base_path = _base_path(base)
    if endpoint == "models":
        path = "/models"
    elif endpoint == "messages" or fmt == "anthropic":
        path = "/messages" if base_path.endswith("/v1") else "/v1/messages"
    elif endpoint == "responses" or fmt == "openai_responses":
        path = "/responses"
    else:
        path = "/chat/completions"
    if base_path.endswith("/v1"):
        return _origin(base) + base_path + path
    return base + path


def auth_headers(account: dict, key: str) -> dict[str, str]:
    """Build outbound probe headers using the configured auth scheme."""
    fmt = str(account.get("api_format") or "openai").strip().lower()
    scheme = str(account.get("auth_header") or "auto").strip().lower()
    if scheme == "auto":
        scheme = "x-api-key" if fmt == "anthropic" else "bearer"
    headers = {"Content-Type": "application/json"}
    if scheme == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + key
    return headers


def model_probe(account: dict, key: str, timeout: float = 15):
    """GET the upstream model catalog with normalized URL/auth settings."""
    import requests

    return requests.get(endpoint_url(account, "models"),
                        headers=auth_headers(account, key), timeout=timeout)


def request_probe(account: dict, key: str, body: dict,
                  timeout: tuple[float, float] = (10, 30)):
    """POST a small protocol probe with the same URL/auth normalization."""
    import requests

    fmt = str(account.get("api_format") or "openai").strip().lower()
    endpoint = "messages" if fmt == "anthropic" else (
        "responses" if fmt == "openai_responses" else "chat")
    return requests.post(endpoint_url(account, endpoint),
                         headers=auth_headers(account, key), json=body,
                         timeout=timeout)
