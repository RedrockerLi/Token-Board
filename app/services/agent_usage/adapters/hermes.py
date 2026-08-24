"""Hermes multi-profile SQLite adapter."""

import os
from pathlib import Path

from ..common import batch, configured_root, make_event, source, sqlite_rows, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "hermes"
LABEL = "Hermes"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".hermes"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    env_root = os.environ.get("HERMES_HOME")
    root = configured_root(software, Path(env_root).expanduser() if env_root else DEFAULT_PATH)
    if root.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or root.is_file():
        return [source(root, profile="default")] if root.is_file() else []
    out = []
    default = root / "state.db"
    if default.is_file():
        out.append(source(default, profile="default"))
    profiles = root / "profiles"
    try:
        for child in profiles.iterdir():
            path = child / "state.db"
            if child.is_dir() and path.is_file():
                out.append(source(path, profile=child.name))
    except OSError:
        pass
    return out


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    rows = sqlite_rows(item.path, """SELECT rowid AS session_rowid,id,model,started_at,
        input_tokens,output_tokens,cache_read_tokens,reasoning_tokens
        FROM sessions WHERE input_tokens > 0 OR output_tokens > 0 ORDER BY rowid""")
    events = []
    for row in rows:
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["session_rowid"],
            model=row["model"] or "unknown", requested_at=timestamp(row["started_at"]),
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
            cached_input_tokens=row["cache_read_tokens"], reasoning_output_tokens=row["reasoning_tokens"],
            project=str(item.context.get("profile") or "default"), session_id=str(row["id"] or "unknown"),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
