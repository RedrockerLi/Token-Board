"""Grok CLI session adapter."""

import json
import logging
import os
from pathlib import Path
from urllib.parse import unquote

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    iter_jsonl,
    make_event,
    project_name,
    read_json,
    safe_float,
    source,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "grok"
LABEL = "Grok"
DEFAULT_PATH = Path.home() / ".grok" / "sessions"
log = logging.getLogger(__name__)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = config_value(software, "data_root", "path")
    direct_sessions = os.environ.get("VIBE_USAGE_GROK_SESSIONS", "").strip()
    if not configured and direct_sessions:
        sessions_roots = [Path(direct_sessions).expanduser()]
    else:
        root = configured or os.environ.get("GROK_HOME")
        sessions = Path(root).expanduser() if root else Path.home() / ".grok"
        if sessions.name != "sessions":
            sessions = sessions / "sessions"
        sessions_roots = [sessions]
    sessions_roots.extend(
        root if root.name == "sessions" else root / "sessions"
        for root in configured_extra_roots(software, KIND)
    )
    candidates = {}
    for sessions in sessions_roots:
        try:
            groups = list(sessions.iterdir())
        except OSError:
            log.debug("Grok discovery root is unavailable", exc_info=True)
            continue
        for group in groups:
            if not group.is_dir():
                continue
            try:
                session_dirs = list(group.iterdir())
            except OSError:
                continue
            for session in session_dirs:
                path = session / "updates.jsonl"
                if not session.is_dir() or not path.is_file():
                    continue
                score = (_file_size(path), _file_size(session / "events.jsonl"),
                         _file_size(session / "summary.json"))
                session_id = session.name
                current = candidates.get(session_id)
                if current is None or score > current[0]:
                    candidates[session_id] = (score, path, session, group)
    return [source(path, session_path=session, session_id=session_id,
                   group=group.name, group_path=group)
            for session_id, (_, path, session, group) in candidates.items()]


def _usage_event(item, ordinal, model, project, ts, usage, session_id):
    if not isinstance(usage, dict):
        return None
    total_input = max(0.0, safe_float(usage.get("inputTokens")))
    cache = max(0.0, safe_float(usage.get("cachedReadTokens")))
    output = max(0.0, safe_float(usage.get("outputTokens")))
    reasoning = max(0.0, safe_float(usage.get("reasoningTokens")))
    model_value = model or "unknown"
    input_tokens = max(0, total_input - cache)
    output_tokens = max(0, output - reasoning)
    return make_event(
        # A session can be copied between Grok homes. Keep its logical id in
        # the event identity so the selected copy and any overlapping scan
        # remain idempotent across roots.
        kind=KIND, source_key=f"session:{session_id}", ordinal=ordinal, model=model_value,
        requested_at=ts, input_tokens=input_tokens,
        output_tokens=output_tokens, cached_input_tokens=cache,
        reasoning_output_tokens=reasoning,
        total_tokens=input_tokens + output_tokens + reasoning,
        project=project, session_id=session_id,
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    session_path = item.context.get("session_path") or item.path.parent
    summary = read_json(Path(session_path) / "summary.json", {})
    if not isinstance(summary, dict):
        summary = {}
    cwd = (summary.get("info") or {}).get("cwd") if isinstance(summary.get("info"), dict) else None
    if cwd:
        project = project_name(cwd)
    else:
        group_path = item.context.get("group_path")
        try:
            group_cwd = (Path(group_path) / ".cwd").read_text(
                encoding="utf-8").strip() if group_path else None
        except OSError:
            group_cwd = None
        if isinstance(group_cwd, str) and group_cwd.strip():
            project = project_name(group_cwd)
        else:
            decoded = unquote(str(item.context.get("group") or "unknown"))
            project = project_name(decoded) if "/" in decoded or "\\" in decoded else decoded
    fallback_model = summary.get("current_model_id") or "unknown"
    events = []
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        update = obj.get("params", {}).get("update") if isinstance(obj.get("params"), dict) else None
        if not isinstance(update, dict):
            continue
        ts = timestamp(obj.get("timestamp"))
        if update.get("sessionUpdate") != "turn_completed":
            continue
        usage = update.get("usage")
        model_usage = usage.get("modelUsage") if isinstance(usage, dict) else None
        if isinstance(model_usage, dict) and model_usage:
            for model, values in model_usage.items():
                event = _usage_event(
                    item, f"{line_no}:{model}", model, project, ts, values,
                    item.context.get("session_id") or item.path.parent.name,
                )
                if event:
                    events.append(event)
        else:
            event = _usage_event(
                item, line_no, fallback_model, project, ts, usage,
                item.context.get("session_id") or item.path.parent.name,
            )
            if event:
                events.append(event)
    return batch(events, count)
