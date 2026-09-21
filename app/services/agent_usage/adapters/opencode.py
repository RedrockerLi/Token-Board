"""OpenCode SQLite/legacy JSON message adapter."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    make_event,
    project_name,
    safe_float,
    source,
    sqlite_rows_snapshot,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "opencode"
LABEL = "OpenCode"
DESCRIPTION = "OpenCode 本地数据库用量"
DEFAULT_PATH_DISPLAY = "~/.local/share/opencode/opencode.db"
DEFAULT_PATH = Path.home() / ".local" / "share" / "opencode"
# SQLite/JSON stores are external and can change without the auth file mtime.
ALWAYS_SCAN = True


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        roots = [Path(configured).expanduser()]
    else:
        override = os.environ.get("VIBE_USAGE_OPENCODE_DIRS", "").strip()
        roots = ([Path(value).expanduser() for value in override.split(os.pathsep)
                  if value.strip()]
                 if override else [DEFAULT_PATH])
    roots.extend(configured_extra_roots(software, KIND))
    return list(dict.fromkeys(roots))


def _store_for_root(root: Path) -> dict | None:
    root = root.expanduser()
    if root.is_file() and root.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return {"format": "sqlite", "path": root}
    if root.is_file():
        return None
    db = root / "opencode.db"
    if db.is_file():
        return {"format": "sqlite", "path": db}
    messages = root if root.name == "message" else root / "storage" / "message"
    if messages.is_dir():
        return {"format": "json", "path": messages}
    return None


def _canonical(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _json_representative(directory: Path) -> Path | None:
    return next(iter(walk_files(directory, (".json",))), None)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    stores = []
    seen = set()
    warnings = []
    warning_paths = []
    configured_extra = {
        _canonical(root) for root in configured_extra_roots(software, KIND)
    }
    for root in _roots(software):
        store = _store_for_root(root)
        if store is None:
            if _canonical(root) in configured_extra:
                warnings.append(f"opencode: 额外数据根目录不可用: {root}")
                warning_paths.append(root)
            continue
        canonical = _canonical(Path(store["path"]))
        if canonical in seen:
            continue
        seen.add(canonical)
        store = {**store, "path": canonical}
        if store["format"] == "json":
            representative = _json_representative(canonical)
            if representative is None:
                continue
        else:
            representative = canonical
        stores.append((store, representative))

    if not stores:
        if warnings:
            return [source(
                warning_paths[0] if warning_paths else _roots(software)[0],
                key="opencode-discovery",
                discovery_warnings=warnings,
            )]
        return []
    representative = stores[0][1]
    return [source(
        representative,
        format="multi",
        stores=[store for store, _ in stores],
        discovery_warnings=warnings,
    )]


def _row_tokens(row: dict) -> dict | None:
    tokens = row.get("tokens")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except (TypeError, ValueError):
            return None
    return tokens if isinstance(tokens, dict) else None


def _token_size(row: dict) -> float:
    tokens = _row_tokens(row) or {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return sum(safe_float(tokens.get(key)) for key in ("input", "output", "reasoning")) \
        + safe_float(cache.get("read")) + safe_float(cache.get("write"))


def _read_sqlite(path: Path) -> list[dict]:
    schema = sqlite_rows_snapshot(
        path, "PRAGMA table_info(message)", raise_on_error=True,
    )
    has_id = any(str(row[1]) == "id" for row in schema)
    identity = "id" if has_id else "rowid"
    rows = sqlite_rows_snapshot(path, f"""SELECT rowid AS message_rowid,
        {identity} AS message_id, session_id AS session_id,
        json_extract(data,'$.role') AS role,
        json_extract(data,'$.time.created') AS created,
        COALESCE(json_extract(data,'$.modelID'),
                 json_extract(data,'$.model.modelID')) AS model_id,
        json_extract(data,'$.tokens') AS tokens,
        json_extract(data,'$.path.root') AS path_root
        FROM message ORDER BY rowid""", raise_on_error=True)
    return [dict(row) for row in rows]


def _read_json(directory: Path) -> list[dict]:
    rows = []
    for path in walk_files(directory, (".json",)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        nested_model = data.get("model") if isinstance(data.get("model"), dict) else {}
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
        rows.append({
            "message_id": data.get("id") or path.stem,
            "session_id": path.parent.name,
            "role": data.get("role"),
            "created": time_data.get("created"),
            "model_id": data.get("modelID") or nested_model.get("modelID"),
            "tokens": data.get("tokens"),
            "path_root": path_data.get("root"),
        })
    return rows


def _event(item: UsageSource, row: dict, physical_key: str):
    tokens = _row_tokens(row)
    if not tokens or row.get("role") != "assistant":
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    session_id = str(row.get("session_id") or "unknown")
    ordinal = row.get("message_id") or row.get("message_rowid") or physical_key
    return make_event(
        kind=KIND,
        source_key=f"session:{session_id}",
        ordinal=ordinal,
        model=row.get("model_id") or "unknown",
        requested_at=row.get("created") if isinstance(row.get("created"), str)
        else timestamp(row.get("created")),
        input_tokens=tokens.get("input", 0),
        output_tokens=tokens.get("output", 0),
        cached_input_tokens=cache.get("read", 0),
        reasoning_output_tokens=tokens.get("reasoning", 0),
        project=project_name(row.get("path_root")),
        session_id=session_id,
        input_includes_cache=True,
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    stores = item.context.get("stores")
    if not isinstance(stores, list):
        stores = [{"format": item.context.get("format"), "path": item.path}]
    warnings = list(item.context.get("discovery_warnings") or [])
    records = {}
    record_count = 0
    for store in stores:
        path = Path(store["path"])
        try:
            rows = (_read_sqlite(path) if store.get("format") == "sqlite"
                    else _read_json(path))
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            warnings.append(f"opencode: 无法读取 {path}: {exc}")
            continue
        record_count += len(rows)
        for index, row in enumerate(rows):
            if stop_event is not None and stop_event.is_set():
                return batch([], record_count, skipped=True, warnings=warnings)
            created = timestamp(row.get("created"))
            if created is None:
                continue
            row = {**row, "created": created}
            session_id = str(row.get("session_id") or "unknown")
            message_id = row.get("message_id")
            if message_id:
                key = (session_id, str(message_id))
            else:
                key = (session_id, f"{path}:{index}")
            old = records.get(key)
            if old is None or _token_size(row) > _token_size(old):
                records[key] = row

    if warnings:
        return batch([], record_count, skipped=True, warnings=warnings)
    events = []
    for row in records.values():
        event = _event(item, row, str(row.get("message_id") or "unknown"))
        if event:
            events.append(event)
    return batch(events, record_count)
