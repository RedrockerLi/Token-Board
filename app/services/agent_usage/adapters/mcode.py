"""MiniMax Code runtime-state SQLite usage adapter."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ..common import (
    batch,
    config_value,
    make_event,
    project_name,
    safe_float,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "mcode"
LABEL = "MiniMax Code"
DESCRIPTION = "MiniMax Code 本地运行时用量"
DEFAULT_PATH_DISPLAY = "~/.minimax/v2/sqlite/runtime-state.sqlite"
DEFAULT_PATH = (
    Path.home() / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite"
)
ALWAYS_SCAN = True

TOKEN_COLUMNS = (
    "session_id",
    "model",
    "ts",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
SESSION_COLUMNS = ("session_id", "workspace_dir", "project_workspace_dir")


def _db_path(software: dict) -> Path:
    configured = config_value(software, "data_root", "path")
    override = configured or os.environ.get("VIBE_USAGE_MCODE_DB", "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("MCODE_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".minimax"
    if home and not root.is_absolute():
        raise ValueError(f"MCODE_HOME 必须是绝对路径: {home!r}")
    return root / "v2" / "sqlite" / "runtime-state.sqlite"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    path = _db_path(software)
    return [source(path)] if path.is_file() else []


def _columns(path: Path, table: str) -> set[str]:
    rows = sqlite_rows_snapshot(
        path, f"PRAGMA table_info({table})", raise_on_error=True,
    )
    return {str(row[1]) for row in rows}


def _number(value) -> float:
    return max(0.0, safe_float(value))


def _valid_schema(path: Path) -> bool:
    return (
        set(TOKEN_COLUMNS).issubset(_columns(path, "local_runtime_token_usage"))
        and set(SESSION_COLUMNS).issubset(_columns(path, "local_runtime_sessions"))
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    path = item.path
    if not path.is_file():
        return batch([], 0)
    try:
        if not _valid_schema(path):
            return batch([], 0, skipped=True,
                         warnings=("mcode: runtime-state 数据库结构不兼容",))
        # Keep the allow-list explicit. Runtime tables also contain raw JSON
        # payloads; selecting them would unnecessarily read message content.
        rows = sqlite_rows_snapshot(path, """
            SELECT
                token.session_id, token.model, token.ts,
                token.input_tokens, token.output_tokens,
                token.reasoning_tokens, token.cache_read_tokens,
                token.cache_write_tokens,
                session.workspace_dir, session.project_workspace_dir,
                token.rowid AS token_rowid
            FROM local_runtime_token_usage AS token
            LEFT JOIN local_runtime_sessions AS session
              ON session.session_id = token.session_id
            ORDER BY token.rowid
        """, raise_on_error=True)
    except (OSError, sqlite3.Error, ValueError):
        return batch([], 0, skipped=True,
                     warnings=("mcode: 无法读取 runtime-state 数据库",))

    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return batch(events, len(rows), skipped=True)
        session_id = str(row[0] or "").strip()
        requested_at = timestamp(row[2])
        if not session_id or requested_at is None:
            continue
        input_tokens = _number(row[3]) + _number(row[7])
        output_tokens = _number(row[4])
        reasoning_tokens = _number(row[5])
        cache_read = _number(row[6])
        if input_tokens + output_tokens + reasoning_tokens + cache_read <= 0:
            continue
        workspace = row[9] or row[8]
        model = str(row[1] or "unknown").strip() or "unknown"
        event = make_event(
            kind=KIND,
            source_key=item.state_key,
            # The runtime ledger has no public event id.  SQLite rowid is the
            # stable physical identity for a token row and avoids changing an
            # event id when an earlier malformed row is filtered out.
            ordinal=f"{session_id}:{row[10]}",
            model=model,
            requested_at=requested_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read,
            reasoning_output_tokens=reasoning_tokens,
            project=project_name(workspace),
            session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
