"""Hermes multi-profile SQLite adapter."""

import os
import logging
import sqlite3
import sys
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_root,
    make_event,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "hermes"
LABEL = "Hermes"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".hermes"
log = logging.getLogger(__name__)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    env_root = os.environ.get("HERMES_HOME")
    if config_value(software, "data_root", "path") or env_root:
        root = configured_root(software, Path(env_root).expanduser() if env_root else DEFAULT_PATH)
    elif sys.platform == "win32":
        # Hermes Desktop/CLI uses LOCALAPPDATA on Windows. Keep the legacy
        # ~/.hermes store only when the native home does not exist yet.
        local_app_data = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        native = local_app_data / "hermes"
        legacy = Path.home() / ".hermes"
        root = legacy if not native.is_dir() and legacy.is_dir() else native
    else:
        root = DEFAULT_PATH
    if root.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or root.is_file():
        return [source(root, profile="default")] if root.is_file() else []
    out = []
    warnings = []
    default = root / "state.db"
    if default.is_file():
        out.append(source(default, profile="default"))
    profiles = root / "profiles"
    try:
        for child in profiles.iterdir():
            path = child / "state.db"
            if not child.is_dir():
                continue
            try:
                if path.is_file():
                    out.append(source(path, profile=child.name))
            except OSError as exc:
                warnings.append(f"hermes: 无法读取 profile {child}: {exc}")
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            warnings.append(f"hermes: 无法读取 profiles 目录 {profiles}: {exc}")
            log.debug("Hermes discovery root is unavailable", exc_info=True)
    if warnings:
        # A profile discovery failure invalidates the complete multi-profile
        # snapshot. Return one warning source so the importer protects every
        # Hermes profile's prior cursor instead of advancing the readable
        # subset and silently dropping the unreadable one.
        warning_path = root if root.is_dir() else (out[0].path if out else root)
        return [source(warning_path, key="hermes-discovery",
                       discovery_warnings=tuple(warnings))]
    return out


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    if not item.path.is_file():
        return batch([], 0)
    try:
        schema = sqlite_rows_snapshot(
            item.path, "PRAGMA table_info(sessions)", raise_on_error=True,
        )
        columns = {str(row[1]) for row in schema}
    except (OSError, sqlite3.Error):
        return batch([], 0, skipped=True,
                     warnings=("hermes: 无法读取 sessions 数据库",))
    required = {"id", "model", "started_at", "input_tokens", "output_tokens",
                "cache_read_tokens", "reasoning_tokens"}
    if not required.issubset(columns):
        return batch([], 0, skipped=True,
                     warnings=("hermes: sessions 数据库结构不兼容",))
    cache_write = "cache_write_tokens" if "cache_write_tokens" in columns else "0"
    try:
        rows = sqlite_rows_snapshot(item.path, f"""SELECT rowid AS session_rowid,id,model,started_at,
            input_tokens,output_tokens,cache_read_tokens,{cache_write} AS cache_write_tokens,
            reasoning_tokens
            FROM sessions
            WHERE input_tokens > 0 OR output_tokens > 0 OR cache_read_tokens > 0
               OR {cache_write} > 0 OR reasoning_tokens > 0 ORDER BY rowid""",
            raise_on_error=True)
    except (OSError, sqlite3.Error):
        return batch([], 0, skipped=True,
                     warnings=("hermes: 无法读取 sessions 数据库",))
    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return batch(events, len(rows), skipped=True)
        output = safe_int(row[5])
        reasoning = min(output, safe_int(row[8]))
        input_tokens = safe_int(row[4]) + safe_int(row[7])
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["session_rowid"],
            model=row["model"] or "unknown", requested_at=timestamp(row["started_at"]),
            input_tokens=input_tokens,
            output_tokens=output - reasoning,
            cached_input_tokens=row["cache_read_tokens"],
            reasoning_output_tokens=reasoning,
            project=str(item.context.get("profile") or "default"), session_id=str(row["id"] or "unknown"),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
