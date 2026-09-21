"""Huawei Cloud CodeArts Agent (CodeArts Doer) usage adapter."""

from __future__ import annotations

import os
from pathlib import Path

from ..common import (
    batch,
    config_value,
    make_event,
    project_name,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "codearts-agent"
LABEL = "CodeArts Agent"
DESCRIPTION = "CodeArts Agent 本地用量"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".codeartsdoer" / "codearts-data" / "opencode.db"

USAGE_SQL = """
  WITH RECURSIVE session_tree(
    sessionId, logicalSessionId, logicalDirectory, isChild, trail
  ) AS (
    SELECT s.id, s.id, s.directory, 0, ',' || s.id || ','
    FROM session AS s
    WHERE s.parent_id IS NULL
      OR NOT EXISTS (SELECT 1 FROM session AS p WHERE p.id = s.parent_id)
    UNION ALL
    SELECT s.id, t.logicalSessionId, t.logicalDirectory, 1,
      t.trail || s.id || ','
    FROM session AS s
    JOIN session_tree AS t ON s.parent_id = t.sessionId
    WHERE instr(t.trail, ',' || s.id || ',') = 0
  ), session_map AS (
    SELECT
      s.id AS sessionId,
      coalesce(t.logicalSessionId, s.id) AS logicalSessionId,
      coalesce(t.logicalDirectory, s.directory) AS logicalDirectory,
      s.directory AS physicalDirectory,
      coalesce(t.isChild, 0) AS isChild
    FROM session AS s
    LEFT JOIN session_tree AS t ON t.sessionId = s.id
  )
  SELECT
    m.id AS messageId,
    m.session_id AS physicalSessionId,
    coalesce(sm.logicalSessionId, m.session_id) AS logicalSessionId,
    coalesce(sm.isChild, 0) AS isChild,
    sm.logicalDirectory AS logicalDirectory,
    sm.physicalDirectory AS physicalDirectory,
    m.time_created AS columnCreated,
    json_extract(m.data, '$.role') AS role,
    json_extract(m.data, '$.time.created') AS dataCreated,
    coalesce(
      json_extract(m.data, '$.modelID'),
      json_extract(m.data, '$.modelId'),
      json_extract(m.data, '$.model.modelID'),
      json_extract(m.data, '$.model.modelId')
    ) AS model,
    json_extract(m.data, '$.path.root') AS rootPath,
    json_extract(m.data, '$.path.cwd') AS cwdPath,
    json_extract(m.data, '$.tokens.input') AS inputTokens,
    json_extract(m.data, '$.tokens.output') AS outputTokens,
    json_extract(m.data, '$.tokens.cache.read') AS cacheReadTokens,
    json_extract(m.data, '$.tokens.cache.write') AS cacheWriteTokens,
    json_extract(m.data, '$.tokens.reasoning') AS reasoningTokens
  FROM message AS m
  LEFT JOIN session_map AS sm ON sm.sessionId = m.session_id
  ORDER BY m.time_created, m.id
"""


def _find_dbs(software: dict | None = None) -> list[Path]:
    roots = []
    if software:
        override = config_value(software, "data_root", "path")
        if override:
            roots.append(Path(override).expanduser())
    env_dirs = os.environ.get("VIBE_USAGE_CODEARTS_AGENT_DIRS", "").strip()
    if env_dirs:
        for part in env_dirs.split(os.pathsep):
            if part.strip():
                roots.append(Path(part.strip()).expanduser())
    if not roots:
        roots.append(DEFAULT_PATH.parent)

    dbs = []
    for root in roots:
        if root.is_file() and root.name == "opencode.db":
            dbs.append(root)
            continue
        candidates = [root / "opencode.db", root / "codearts-data" / "opencode.db"]
        for cand in candidates:
            if cand.is_file():
                dbs.append(cand)
                break
    return list(dict.fromkeys(dbs))


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return [source(db_path) for db_path in _find_dbs(software)]


def _resolve_ts(data_created, col_created) -> str | None:
    raw = data_created if data_created is not None else col_created
    if raw is None:
        return None
    return timestamp(raw)


def _token_footprint(row) -> int:
    return (
        safe_int(row["inputTokens"])
        + safe_int(row["outputTokens"])
        + safe_int(row["cacheReadTokens"])
        + safe_int(row["cacheWriteTokens"])
        + safe_int(row["reasoningTokens"])
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    if not item.path.is_file():
        return batch([], 0)

    try:
        rows = sqlite_rows_snapshot(item.path, USAGE_SQL, raise_on_error=True)
    except Exception as err:
        return batch([], 0, skipped=True, warnings=(f"codearts-agent: cannot read {item.path}: {err}",))

    records: dict[str, dict] = {}
    for index, row in enumerate(rows):
        if stop_event is not None and stop_event.is_set():
            break

        ts = _resolve_ts(row["dataCreated"], row["columnCreated"])
        if not ts:
            continue

        session_id = str(row["physicalSessionId"] or "")
        message_id = str(row["messageId"] or "")
        key = f"{session_id}|{message_id}" if session_id and message_id else f"{item.path}:{index}"

        footprint = _token_footprint(row)
        prev = records.get(key)
        if not prev or footprint > prev["footprint"]:
            records[key] = {
                "row": row,
                "timestamp": ts,
                "footprint": footprint,
                "ordinal": f"{session_id}:{message_id}" if session_id and message_id else f"idx:{index}",
            }

    events = []
    for rec in records.values():
        row = rec["row"]
        if row["role"] != "assistant":
            continue

        input_tokens = safe_int(row["inputTokens"]) + safe_int(row["cacheWriteTokens"])
        output_tokens = safe_int(row["outputTokens"])
        cache_read = safe_int(row["cacheReadTokens"])
        reasoning = safe_int(row["reasoningTokens"])
        if input_tokens + output_tokens + cache_read + reasoning == 0:
            continue

        raw_dir = row["rootPath"] or row["cwdPath"] or row["logicalDirectory"] or row["physicalDirectory"]
        project = project_name(raw_dir, "unknown") if raw_dir else "unknown"
        logical_session = str(row["logicalSessionId"] or row["physicalSessionId"] or "unknown")

        model = str(row["model"] or "unknown").strip() or "unknown"

        event = make_event(
            kind=KIND,
            source_key=item.state_key,
            ordinal=rec["ordinal"],
            model=model,
            requested_at=rec["timestamp"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read,
            reasoning_output_tokens=reasoning,
            project=project,
            session_id=logical_session,
        )
        if event:
            events.append(event)

    return batch(events, len(rows))
