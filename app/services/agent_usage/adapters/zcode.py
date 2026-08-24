"""ZCode SQLite message adapter."""

import json
from pathlib import Path

from ..common import batch, configured_root, make_event, project_name, source, sqlite_rows, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "zcode"
LABEL = "ZCode"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = configured_root(software, DEFAULT_PATH)
    path = root if root.suffix in {".db", ".sqlite", ".sqlite3"} or root.is_file() else root / "db.sqlite"
    return [source(path)] if path.is_file() else []


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    rows = sqlite_rows(item.path, """SELECT m.rowid AS message_rowid,
        m.session_id AS session_id, m.time_created AS created,
        json_extract(m.data,'$.role') AS role,
        json_extract(m.data,'$.modelID') AS model_id,
        json_extract(m.data,'$.tokens') AS tokens,
        json_extract(m.data,'$.path.root') AS path_root,
        json_extract(m.data,'$.path.cwd') AS path_cwd,
        s.directory AS session_dir
        FROM message m LEFT JOIN session s ON s.id=m.session_id ORDER BY m.rowid""")
    events = []
    for row in rows:
        if row["role"] != "assistant":
            continue
        try:
            tokens = json.loads(row["tokens"]) if isinstance(row["tokens"], str) else row["tokens"]
        except (TypeError, ValueError):
            continue
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["message_rowid"], model=row["model_id"] or "unknown",
            requested_at=timestamp(row["created"]), input_tokens=max(0, float(tokens.get("input", 0) or 0) - float(cache.get("read", 0) or 0)) + float(cache.get("write", 0) or 0),
            output_tokens=max(0, float(tokens.get("output", 0) or 0) - float(tokens.get("reasoning", 0) or 0)),
            cached_input_tokens=cache.get("read", 0), reasoning_output_tokens=tokens.get("reasoning", 0),
            project=project_name(row["path_root"] or row["path_cwd"] or row["session_dir"]), session_id=str(row["session_id"] or "unknown"),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
