"""Shared management-plane upstream URL and authentication helpers.

The C++ proxy owns request forwarding.  These helpers are only for dashboard
probes (model discovery and the concurrency check), but they deliberately use
the same endpoint/auth normalization as the runtime so a configured Anthropic
or custom-path upstream is not accidentally probed with Bearer credentials.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from app.domain.account_types import subtype_spec


def _origin(base_url: str) -> str:
    parts = urlsplit((base_url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError("上游 Base URL 必须包含协议和主机名")
    return urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")


def _base_path(base_url: str) -> str:
    return urlsplit((base_url or "").strip()).path.rstrip("/")


def endpoint_url(account: dict, endpoint: str,
                 model: str | None = None) -> str:
    """Resolve a management endpoint using account endpoint semantics."""
    base = str(account.get("base_url") or "").rstrip("/")
    configured = str(account.get("endpoint_path") or "").strip()
    if endpoint not in {"models", "chat", "messages", "responses", "generate"}:
        raise ValueError(f"unsupported probe endpoint: {endpoint}")
    if configured:
        if configured.startswith(("http://", "https://")):
            target = configured
        else:
            target = _origin(base) + "/" + configured.lstrip("/")
        if model:
            bare = model[len("models/"):] if model.startswith("models/") else model
            target = target.replace("{model}", bare)
        return target.replace("{method}", "generateContent")

    fmt = str(account.get("api_format") or "openai").strip().lower()
    base_path = _base_path(base)
    if fmt in ("google", "gemini"):
        # Gemini names the model in the URL and expects the API version to be
        # part of the base URL; add the documented prefix when it is absent.
        versioned = base_path.rsplit("/", 1)[-1] in ("v1", "v1beta", "v1alpha")
        root = base if versioned else base + "/v1beta"
        if endpoint == "models":
            return root + "/models"
        if not model:
            raise ValueError("Gemini 请求端点需要模型名")
        bare = model[len("models/"):] if model.startswith("models/") else model
        return root + "/models/" + bare + ":generateContent"
    if endpoint == "models":
        # A subtype may declare its own catalog path (opencode-go's gateway
        # serves the model list at /v1/models); everything else keeps the
        # plain "{base}/models" shape, whatever wire format was chosen.
        subtype = account.get("subtype")
        declared = subtype_spec(
            str(subtype).strip() if subtype else None)
        if declared is not None and declared.models_path:
            return base + declared.models_path
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
        scheme = ("x-api-key" if fmt == "anthropic"
                  else "x-goog-api-key" if fmt in ("google", "gemini")
                  else "bearer")
    headers = {"Content-Type": "application/json"}
    if scheme == "x-api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif scheme == "x-goog-api-key":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    return headers


def parse_model_catalog(api_format: str, payload) -> list[str]:
    """Model ids from an upstream catalog body, by wire shape.

    OpenAI-family upstreams answer `{"data":[{"id":...}]}`; Gemini answers
    `{"models":[{"name":"models/<id>"}]}`.
    """
    if not isinstance(payload, dict):
        return []
    fmt = str(api_format or "openai").strip().lower()
    if fmt in ("google", "gemini"):
        entries = payload.get("models")
        if not isinstance(entries, list):
            return []
        models: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("baseModelId")
            if not isinstance(name, str) or not name:
                continue
            models.append(name[len("models/"):]
                          if name.startswith("models/") else name)
        return models
    entries = payload.get("data")
    if not isinstance(entries, list):
        return []
    return [entry["id"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def model_probe(account: dict, key: str, timeout: float = 15):
    """GET the upstream model catalog with normalized URL/auth settings."""
    import requests

    return requests.get(endpoint_url(account, "models"),
                        headers=auth_headers(account, key), timeout=timeout)


def request_probe(account: dict, key: str, body: dict,
                  timeout: tuple[float, float] = (10, 30),
                  model: str | None = None):
    """POST a small protocol probe with the same URL/auth normalization."""
    import requests

    fmt = str(account.get("api_format") or "openai").strip().lower()
    if fmt in ("google", "gemini"):
        endpoint = "generate"
    else:
        endpoint = "messages" if fmt == "anthropic" else (
            "responses" if fmt == "openai_responses" else "chat")
    return requests.post(endpoint_url(account, endpoint, model=model),
                         headers=auth_headers(account, key), json=body,
                         timeout=timeout)
