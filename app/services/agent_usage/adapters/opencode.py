"""OpenCode SQLite/legacy JSON message adapter."""

import json
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, read_json, source, sqlite_rows, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "opencode"
LABEL = "OpenCode"
DESCRIPTION = "OpenCode 本地数据库用量"
DEFAULT_PATH_DISPLAY = "~/.local/share/opencode/opencode.db"
DEFAULT_PATH = Path.home() / ".local" / "share" / "opencode"
ALWAYS_SCAN = True


def _paths(software: dict) -> tuple[Path, Path]:
    configured = config_value(software, "data_root", "path")
    root = Path(configured).expanduser() if configured else DEFAULT_PATH
    if root.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or root.is_file():
        return root, root.parent / "storage" / "message"
    return root / "opencode.db", root / "storage" / "message"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    db, messages = _paths(software)
    if db.is_file():
        return [source(db, format="sqlite", legacy_dir=messages)]
    return [source(path, format="json", session_id=path.parent.name)
            for path in walk_files(messages, (".json",))]


def _event(item: UsageSource, ordinal, data: dict, session_id: str) -> object:
    tokens = data.get("tokens")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except (TypeError, ValueError):
            return None
    if not isinstance(tokens, dict) or not data.get("modelID"):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    created = data.get("time", {}).get("created") if isinstance(data.get("time"), dict) else None
    return make_event(
        # A message can move from the legacy JSON store into SQLite.  Keep the
        # logical session in the event id so that migration does not make the
        # same message bill twice merely because its physical source changed.
        kind=KIND, source_key=f"session:{session_id}", ordinal=ordinal, model=data.get("modelID"),
        requested_at=timestamp(created), input_tokens=tokens.get("input", 0), output_tokens=tokens.get("output", 0),
        cached_input_tokens=cache.get("read", 0), reasoning_output_tokens=tokens.get("reasoning", 0),
        project=project_name(data.get("path", {}).get("root") if isinstance(data.get("path"), dict) else None),
        session_id=session_id, input_includes_cache=True,
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    if item.context.get("format") == "sqlite" or item.path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        rows = sqlite_rows(item.path, """SELECT rowid AS message_rowid,session_id,
            json_extract(data,'$.role') AS role,
            json_extract(data,'$.time.created') AS created,
            json_extract(data,'$.modelID') AS model_id,
            json_extract(data,'$.tokens') AS tokens,
            json_extract(data,'$.path.root') AS root_path FROM message ORDER BY rowid""")
        for row in rows:
            if row["role"] != "assistant":
                continue
            data = {"time": {"created": row["created"]}, "modelID": row["model_id"], "tokens": row["tokens"], "path": {"root": row["root_path"]}}
            event = _event(item, row["message_rowid"], data, str(row["session_id"] or "unknown"))
            if event:
                events.append(event)
        if rows or not item.context.get("legacy_dir"):
            return batch(events, len(rows))
        # SQLite was introduced after the JSON message store.  A partially
        # migrated/locked DB can exist beside valid legacy messages; preserve
        # the reference parser's graceful fallback instead of returning zero.
        legacy_events = []
        count = 0
        for path in walk_files(Path(item.context["legacy_dir"]), (".json",)):
            data = read_json(path)
            count += 1
            if not isinstance(data, dict):
                continue
            event = _event(
                source(path, format="json", session_id=path.parent.name),
                path.stem, data, path.parent.name,
            )
            if event:
                legacy_events.append(event)
        return batch(legacy_events, count)
    data = read_json(item.path)
    if isinstance(data, dict):
        event = _event(item, item.path.stem, data, str(item.context.get("session_id") or item.path.parent.name))
        if event:
            events.append(event)
    return batch(events, 1 if data is not None else 0)
