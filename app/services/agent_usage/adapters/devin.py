"""Devin CLI and Desktop shared WAL SQLite database adapter."""

from __future__ import annotations

import os
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_root,
    make_event,
    project_name,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "devin"
LABEL = "Devin"
DESCRIPTION = "Devin 本地用量"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"

NODE_COLUMNS = {"session_id", "node_id", "chat_message", "created_at"}
SESSION_COLUMNS = {"id", "working_directory", "model"}

USAGE_SQL = """
  SELECT
    m.session_id AS sessionId,
    m.row_id AS rowId,
    m.created_at AS nodeCreated,
    json_extract(m.chat_message, '$.message_id') AS messageId,
    json_extract(m.chat_message, '$.role') AS role,
    json_extract(m.chat_message, '$.metadata.is_user_input') AS isUserInput,
    json_extract(m.chat_message, '$.metadata.created_at') AS msgCreatedAt,
    json_extract(m.chat_message, '$.metadata.generation_model') AS generationModel,
    json_extract(m.chat_message, '$.metadata.metrics.input_tokens') AS inputTokens,
    json_extract(m.chat_message, '$.metadata.metrics.output_tokens') AS outputTokens,
    json_extract(m.chat_message, '$.metadata.metrics.cache_read_tokens') AS cacheReadTokens,
    json_extract(m.chat_message, '$.metadata.metrics.cache_creation_tokens') AS cacheCreationTokens,
    s.working_directory AS workingDir,
    s.model AS sessionModel
  FROM message_nodes AS m
  LEFT JOIN sessions AS s ON s.id = m.session_id
"""


def resolve_devin_db_path(software: dict | None = None) -> Path:
    if software:
        override = config_value(software, "data_root", "path")
        if override:
            root = Path(override).expanduser()
            if root.is_file() or root.suffix.lower() == ".db":
                return root
            return root / "sessions.db"
    env_db = os.environ.get("VIBE_USAGE_DEVIN_DB", "").strip()
    if env_db:
        return Path(env_db).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip() or str(Path.home() / ".local" / "share")
    return Path(data_home) / "devin" / "cli" / "sessions.db"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    path = resolve_devin_db_path(software)
    return [source(path)] if path.is_file() else []


def _resolve_timestamp(row) -> str | None:
    msg_created = row["msgCreatedAt"]
    if msg_created:
        ts = timestamp(msg_created)
        if ts:
            return ts
    node_created = row["nodeCreated"]
    if node_created is not None:
        try:
            val = float(node_created)
            if val > 0:
                return timestamp(val)
        except (TypeError, ValueError):
            pass
    return None


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    if not item.path.is_file():
        return batch([], 0)

    # Schema guard
    try:
        table_info_nodes = sqlite_rows_snapshot(item.path, "PRAGMA table_info(message_nodes)", raise_on_error=True)
        nodes_cols = {str(r["name"]) for r in table_info_nodes}
        if not NODE_COLUMNS.issubset(nodes_cols):
            return batch([], 0, skipped=True, warnings=(f"devin: table message_nodes missing required columns in {item.path}",))

        table_info_sessions = sqlite_rows_snapshot(item.path, "PRAGMA table_info(sessions)", raise_on_error=True)
        sessions_cols = {str(r["name"]) for r in table_info_sessions}
        if not SESSION_COLUMNS.issubset(sessions_cols):
            return batch([], 0, skipped=True, warnings=(f"devin: table sessions missing required columns in {item.path}",))

        rows = sqlite_rows_snapshot(item.path, USAGE_SQL, raise_on_error=True)
    except Exception as err:
        return batch([], 0, skipped=True, warnings=(f"devin: cannot query {item.path}: {err}",))

    seen = set()
    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            break

        session_id = str(row["sessionId"] or "").strip()
        if not session_id:
            continue

        message_id = str(row["messageId"] or f"row:{row['rowId']}")
        dedup_key = f"{session_id}|{message_id}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        if row["role"] != "assistant":
            continue

        ts = _resolve_timestamp(row)
        if not ts:
            continue

        input_tokens = safe_int(row["inputTokens"]) + safe_int(row["cacheCreationTokens"])
        cached_tokens = safe_int(row["cacheReadTokens"])
        output_tokens = safe_int(row["outputTokens"])
        if input_tokens + cached_tokens + output_tokens == 0:
            continue

        model = str(row["generationModel"] or row["sessionModel"] or "unknown").strip() or "unknown"
        working_dir = row["workingDir"]
        project = project_name(working_dir, "unknown") if working_dir else "unknown"

        event = make_event(
            kind=KIND,
            source_key=item.state_key,
            ordinal=dedup_key,
            model=model,
            requested_at=ts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            project=project,
            session_id=session_id,
        )
        if event:
            events.append(event)

    return batch(events, len(rows))
