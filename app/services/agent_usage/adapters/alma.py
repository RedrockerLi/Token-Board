"""Alma Electron usage ledger adapter."""

import os
import sys
from pathlib import Path

from ..common import batch, config_value, configured_root, make_event, project_name, safe_int, source, sqlite_rows, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "alma"
LABEL = "Alma"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".config" / "alma" / "chat_threads.db"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    override = config_value(software, "data_root", "path") or os.environ.get("VIBE_USAGE_ALMA_DB")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "alma" / "chat_threads.db"
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "alma" / "chat_threads.db"
    else:
        root = configured_root(software, DEFAULT_PATH)
    path = root if root.suffix.lower() == ".db" or root.is_file() else root / "chat_threads.db"
    return [source(path)] if path.is_file() else []


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    rows = sqlite_rows(item.path, """SELECT rowid AS usage_rowid,
        usage_records.model AS model, usage_records.timestamp AS timestamp,
        usage_records.input_tokens AS input_tokens,
        usage_records.output_tokens AS output_tokens,
        usage_records.cached_input_tokens AS cached_input_tokens,
        usage_records.reasoning_tokens AS reasoning_tokens,
        usage_records.cache_write_input_tokens AS cache_write_input_tokens,
        workspaces.name AS workspace_name
        FROM usage_records
        LEFT JOIN chat_threads ON chat_threads.id = usage_records.thread_id
        LEFT JOIN workspaces ON workspaces.id = chat_threads.workspace_id
        ORDER BY usage_records.rowid""")
    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return batch(events, len(rows))
        model = str(row["model"] or "unknown").strip() or "unknown"
        if ":" in model:
            model = model.rsplit(":", 1)[-1].strip() or "unknown"
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["usage_rowid"],
            model=model, requested_at=timestamp(row["timestamp"]),
            input_tokens=safe_int(row["input_tokens"]) + safe_int(row["cache_write_input_tokens"]),
            output_tokens=row["output_tokens"], cached_input_tokens=row["cached_input_tokens"],
            reasoning_output_tokens=row["reasoning_tokens"],
            project=project_name(row["workspace_name"]),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
