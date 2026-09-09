"""Cursor cloud usage CSV adapter.

Cursor keeps only an auth token locally; the usage rows are obtained from its
dashboard export.  This adapter intentionally uses stdlib HTTP so the Python
dashboard has no extra dependency.
"""

import base64
import errno as errno_codes
import json
import logging
import os
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..common import (
    batch,
    config_value,
    csv_rows,
    make_event,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

log = logging.getLogger(__name__)

KIND = "cursor"
LABEL = "Cursor"
DEFAULT_PATH = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
# Cursor usage is fetched from the cloud.  The local auth database's mtime is
# not a reliable indication that the remote export changed.
ALWAYS_SCAN = True


def _db(software: dict) -> Path:
    configured = config_value(software, "data_root", "path") or os.environ.get("CURSOR_STATE_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    config_dir = os.environ.get("CURSOR_CONFIG_DIR", "").strip()
    if config_dir:
        candidates = []
        for raw in config_dir.split(","):
            value = Path(raw.strip()).expanduser()
            candidates.append(value if value.suffix == ".vscdb" else value / "User" / "globalStorage" / "state.vscdb")
        return next((value for value in candidates if value.is_file()), candidates[0])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    path = _db(software)
    return [source(path)] if path.is_file() else []


def _token(db: Path) -> str | None:
    rows = sqlite_rows_snapshot(
        db, "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken' LIMIT 1",
        raise_on_error=True,
    )
    value = rows[0][0] if rows else None
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _cookie_value(token: str) -> str:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        sub = str(decoded.get("sub") or "").strip()
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        sub = ""
    return f"{sub}%3A%3A{token}" if sub else token


def _cookie_values(token: str) -> list[str]:
    """Return the browser and legacy Cursor cookie formats in priority order."""
    values = [_cookie_value(token)]
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        sub = str(decoded.get("sub") or "").strip()
        if "|" in sub:
            values.append(f"{sub.rsplit('|', 1)[-1]}%3A%3A{token}")
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        log.debug("Cursor token payload did not contain a project", exc_info=True)
    values.append(token)
    return list(dict.fromkeys(values))


DEFAULT_FETCH_TIMEOUT_MS = 30_000
MAX_FETCH_TIMEOUT_MS = 2_147_483_647


def resolve_cursor_fetch_timeout(value) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_FETCH_TIMEOUT_MS
    if not parsed.is_integer() or not 0 < parsed <= MAX_FETCH_TIMEOUT_MS:
        return DEFAULT_FETCH_TIMEOUT_MS
    return int(parsed)


def _network_warning(exc: BaseException, timeout_ms: int) -> str:
    codes = []
    seen = set()
    timed_out = False

    def visit(value) -> None:
        nonlocal timed_out
        if value is None or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, TimeoutError):
            timed_out = True
        code = getattr(value, "errno", None) or getattr(value, "code", None)
        if isinstance(code, int):
            code = errno_codes.errorcode.get(code) or next(
                (name for name in ("EAI_AGAIN", "EAI_NONAME", "EAI_FAIL")
                 if getattr(socket, name, None) == code),
                None,
            )
            if code == "EAI_NONAME":
                code = "ENOTFOUND"
        if isinstance(code, str) and code not in codes:
            codes.append(code)
        reason = getattr(value, "reason", None)
        if isinstance(reason, BaseException):
            visit(reason)
        elif isinstance(reason, str) and "timed out" in reason.lower():
            timed_out = True
        cause = getattr(value, "__cause__", None)
        if isinstance(cause, BaseException):
            visit(cause)

    visit(exc)
    code = codes[0] if codes else None
    if isinstance(exc, HTTPError):
        detail = f"HTTP {exc.code}"
    elif timed_out or any(value in {"ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT"}
                          for value in codes):
        detail = f"timeout after {timeout_ms}ms"
    elif isinstance(code, str):
        detail = ", ".join(codes)
    else:
        detail = "network error"
    if "timeout" in detail.lower():
        hint = "Cursor 用量导出超时，请稍后重试；可通过 VIBE_USAGE_CURSOR_FETCH_TIMEOUT_MS 增大等待时间。"
    elif any(value in {"ENOTFOUND", "EAI_AGAIN"} for value in codes):
        hint = "请检查 DNS 和终端代理配置。"
    elif any("CERT" in value or value in {
            "SELF_SIGNED_CERT_IN_CHAIN", "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
            "ERR_TLS_CERT_ALTNAME_INVALID",
    } for value in codes):
        hint = "请检查系统时间及代理或公司网络的 CA 证书配置。"
    else:
        hint = "请检查终端网络和代理配置。"
    return f"cursor: 用量导入暂跳过（{detail}）。{hint}"


def _csv_int(value) -> int:
    return safe_int(str(value).replace(",", ""))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    try:
        token = _token(item.path)
    except (OSError, sqlite3.Error):
        return batch([], 0, skipped=True,
                     warnings=("cursor: 无法读取本地会话数据库，已跳过本次用量导入。",))
    if not token:
        return batch([], 0)
    base = os.environ.get("CURSOR_WEB_BASE_URL", "https://cursor.com").rstrip("/")
    url = f"{base}/api/dashboard/export-usage-events-csv?strategy=tokens"
    base_headers = {
        "Accept": "text/csv,*/*;q=0.8", "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard?tab=usage",
        "User-Agent": "Mozilla/5.0",
    }
    text = None
    timeout_ms = resolve_cursor_fetch_timeout(
        os.environ.get("VIBE_USAGE_CURSOR_FETCH_TIMEOUT_MS")
    )
    attempts = [
        {"Cookie": f"WorkosCursorSessionToken={value}"}
        for value in _cookie_values(token)
    ] + [{"Authorization": f"Bearer {token}"}]
    for auth_headers in attempts:
        request = Request(url, headers={**base_headers, **auth_headers})
        try:
            with urlopen(request, timeout=timeout_ms / 1000) as response:
                text = response.read().decode("utf-8", errors="replace")
            break
        except HTTPError as exc:
            if exc.code in {401, 403}:
                continue
            return batch([], 0, skipped=True,
                         warnings=(_network_warning(exc, timeout_ms),))
        except (OSError, URLError, TimeoutError) as exc:
            return batch([], 0, skipped=True,
                         warnings=(_network_warning(exc, timeout_ms),))
    if text is None:
        raise RuntimeError(
            "cursor: 会话凭据已被拒绝，请在 Cursor 中重新登录后重试。"
        )
    events = []
    rows = list(csv_rows(text))
    for index, row in enumerate(rows):
        model = row.get("Model", "").strip()
        ts = timestamp(row.get("Date"))
        if not model or not ts:
            continue
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=f"{index}:{row.get('Date')}:{model}", model=model,
            requested_at=ts, input_tokens=_csv_int(row.get("Input (w/ Cache Write)")) + _csv_int(row.get("Input (w/o Cache Write)")),
            output_tokens=_csv_int(row.get("Output Tokens")), cached_input_tokens=_csv_int(row.get("Cache Read")),
            project="unknown", input_includes_cache=False,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
