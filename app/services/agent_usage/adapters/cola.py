"""Cola 1.4.4 Pi-compatible session adapter."""

from __future__ import annotations

import os
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_root,
    iter_jsonl,
    make_event,
    project_name,
    safe_int,
    source,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "cola"
LABEL = "Cola"
DESCRIPTION = "Cola 本地用量"
DEFAULT_PATH = Path.home() / ".cola" / "sessions"


def get_sessions_dir(software: dict | None = None) -> Path:
    if software:
        override = config_value(software, "data_root", "path")
        if override:
            root = Path(override).expanduser()
            return root if root.name == "sessions" or not (root / "sessions").is_dir() else root / "sessions"
    env_dir = os.environ.get("VIBE_USAGE_COLA_SESSIONS", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    data_dir = os.environ.get("COLA_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "sessions"
    return DEFAULT_PATH


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    sessions_dir = get_sessions_dir(software)
    if not sessions_dir.is_dir():
        return []
    sources = []
    try:
        for path in walk_files(sessions_dir, (".jsonl",)):
            sources.append(source(path, sessions_root=sessions_dir))
    except OSError as err:
        return [source(
            sessions_dir,
            key=f"{KIND}-discovery",
            discovery_warnings=(f"cola: 无法读取会话目录 {sessions_dir}: {err}",),
        )]
    return sources


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    if not item.path.is_file():
        return batch([], 0)

    events = []
    session_id = item.path.stem
    project = "unknown"
    count = 0

    try:
        for line_no, obj in iter_jsonl(item.path):
            count = line_no
            if stop_event is not None and stop_event.is_set():
                break
            if not isinstance(obj, dict):
                continue
            record_type = obj.get("type")
            if record_type == "session":
                session_id = str(obj.get("id") or session_id)
                # Scope slugs may identify channels or people, not projects.
                # Only the session header's cwd supplies a project.
                cwd = obj.get("cwd")
                if cwd:
                    project = project_name(cwd, "unknown")
                continue
            if record_type != "message":
                continue
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("role") != "assistant":
                continue

            ts = timestamp(obj.get("timestamp") or message.get("timestamp"))
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            cache_read = safe_int(usage.get("cacheRead", usage.get("cacheReadInputTokens", 0)))
            cache_write = safe_int(usage.get("cacheWrite", usage.get("cacheCreationInputTokens", 0)))
            reasoning = safe_int(usage.get("reasoning", usage.get("reasoningTokens", 0)))
            raw_input = safe_int(usage.get("input", usage.get("inputTokens", 0)))
            raw_output = safe_int(usage.get("output", usage.get("outputTokens", 0)))

            input_tokens = raw_input + cache_write
            output_tokens = max(0, raw_output - reasoning)
            model = str(message.get("model") or obj.get("model") or "unknown").strip() or "unknown"
            record_id = obj.get("id")

            event = make_event(
                kind=KIND,
                source_key=session_id if record_id is not None else item.state_key,
                ordinal=record_id if record_id is not None else line_no,
                model=model,
                requested_at=ts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cache_read,
                reasoning_output_tokens=reasoning,
                project=project,
                session_id=session_id,
            )
            if event:
                events.append(event)
    except OSError as err:
        return batch([], 0, skipped=True, warnings=(f"cola: cannot read {item.path}: {err}",))

    return batch(events, count)
