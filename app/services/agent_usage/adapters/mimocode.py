"""MiMoCode SQLite message adapter."""

import json
import os
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, read_json, source, sqlite_rows, sqlite_table_exists, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "mimocode"
LABEL = "MiMoCode"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".local" / "share" / "mimocode" / "mimocode.db"


def _db(software: dict) -> Path:
    configured = (config_value(software, "data_root", "path")
                  or os.environ.get("MIMOCODE_DB")
                  or os.environ.get("VIBE_USAGE_MIMOCODE_DB"))
    if configured:
        path = Path(configured).expanduser()
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or path.is_file():
            return path
        return path / "mimocode.db"
    home = os.environ.get("MIMOCODE_HOME")
    if home:
        return Path(home).expanduser() / "data" / "mimocode.db"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mimocode" / "mimocode.db"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    path = _db(software)
    return [source(path)] if path.is_file() else []


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    external = sqlite_table_exists(item.path, "external_import")
    join = "LEFT JOIN external_import ON external_import.session_id=message.session_id" if external else ""
    where = "WHERE external_import.session_id IS NULL" if external else ""
    rows = sqlite_rows(item.path, f"""SELECT message.rowid AS message_rowid,
        message.session_id AS session_id, message.time_created AS created,
        message.data AS data, session.directory AS directory
        FROM message JOIN session ON session.id=message.session_id {join} {where}
        ORDER BY message.rowid""")
    events = []
    for row in rows:
        try:
            data = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("role") != "assistant":
            continue
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            continue
        cache = (tokens.get("cache") or {}) if isinstance(tokens.get("cache"), dict) else {}
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["message_rowid"],
            model=data.get("modelID", "unknown"), requested_at=timestamp(data.get("time", {}).get("created") if isinstance(data.get("time"), dict) else row["created"]),
            input_tokens=tokens.get("input", 0) + cache.get("write", 0), output_tokens=tokens.get("output", 0),
            cached_input_tokens=cache.get("read", 0), reasoning_output_tokens=tokens.get("reasoning", 0),
            project=project_name(row["directory"]), session_id=str(row["session_id"] or "unknown"),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
