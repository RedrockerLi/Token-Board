"""Cursor cloud usage CSV adapter.

Cursor keeps only an auth token locally; the usage rows are obtained from its
dashboard export.  This adapter intentionally uses stdlib HTTP so the Python
dashboard has no extra dependency.
"""

import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..common import batch, config_value, csv_rows, make_event, source, sqlite_rows, safe_int, timestamp
from ..ir import ParseBatch, UsageSource

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
    rows = sqlite_rows(db, "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken' LIMIT 1")
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
        pass
    values.append(token)
    return list(dict.fromkeys(values))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    token = _token(item.path)
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
    attempts = [
        {"Cookie": f"WorkosCursorSessionToken={value}"}
        for value in _cookie_values(token)
    ] + [{"Authorization": f"Bearer {token}"}]
    for auth_headers in attempts:
        request = Request(url, headers={**base_headers, **auth_headers})
        try:
            with urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", errors="replace")
            break
        except HTTPError as exc:
            if exc.code in {401, 403}:
                continue
            return batch([], 0)
        except OSError:
            return batch([], 0)
    if text is None:
        return batch([], 0)
    events = []
    rows = list(csv_rows(text))
    for index, row in enumerate(rows):
        model = row.get("Model", "").strip()
        ts = timestamp(row.get("Date"))
        if not model or not ts:
            continue
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=f"{index}:{row.get('Date')}:{model}", model=model,
            requested_at=ts, input_tokens=safe_int(row.get("Input (w/ Cache Write)")) + safe_int(row.get("Input (w/o Cache Write)")),
            output_tokens=safe_int(row.get("Output Tokens")), cached_input_tokens=safe_int(row.get("Cache Read")),
            project="unknown", input_includes_cache=False,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
