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
    safe_int,
    source,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "grok"
LABEL = "Grok"
DEFAULT_PATH = Path.home() / ".grok" / "sessions"
ALWAYS_SCAN = True
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
    extra_roots = configured_extra_roots(software, KIND)
    sessions_roots.extend(
        root if root.name == "sessions" else root / "sessions"
        for root in extra_roots
    )
    candidates = {}
    discovery_warnings = []
    for root_index, sessions in enumerate(sessions_roots):
        strict = root_index >= len(sessions_roots) - len(extra_roots)
        try:
            groups = list(sessions.iterdir())
        except OSError:
            log.debug("Grok discovery root is unavailable", exc_info=True)
            if strict:
                discovery_warnings.append(
                    f"grok: 额外数据根目录不可用，已保留上次同步状态: {sessions}"
                )
            continue
        for group in groups:
            if not group.is_dir():
                continue
            try:
                session_dirs = list(group.iterdir())
            except OSError:
                if strict:
                    discovery_warnings.append(
                        f"grok: 额外数据根目录读取失败，已保留上次同步状态: {sessions}"
                    )
                continue
            for session in session_dirs:
                if not session.is_dir():
                    continue
                update_path = session / "updates.jsonl"
                ledger_path = session / "usage.json"
                summary_path = session / "summary.json"
                if update_path.is_file():
                    path = update_path
                elif ledger_path.is_file():
                    path = ledger_path
                elif summary_path.is_file():
                    path = summary_path
                else:
                    continue
                score = (_file_size(update_path), _file_size(ledger_path),
                         _file_size(session / "events.jsonl"),
                         _file_size(session / "summary.json"))
                session_id = session.name
                current = candidates.get(session_id)
                if current is None or score > current[0]:
                    candidates[session_id] = (score, path, session, group)
    if discovery_warnings:
        warning_path = extra_roots[0] if extra_roots else DEFAULT_PATH
        return [source(
            warning_path,
            key="grok-discovery",
            discovery_warnings=tuple(dict.fromkeys(discovery_warnings)),
        )]
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
        project=project, session_id=session_id,
    )


def _ledger_usage(record: dict) -> dict | None:
    """Normalize Grok 1.0's usage.json record to the ACP usage shape."""
    if not isinstance(record, dict):
        return None

    def fold(value):
        if not isinstance(value, dict):
            return value
        input_tokens = max(0.0, safe_float(value.get("inputTokens")))
        cache_creation = max(0.0, safe_float(value.get("cacheCreationTokens")))
        return {**value, "inputTokens": input_tokens + cache_creation}

    model_usage = record.get("modelUsage")
    usage = fold(record)
    if isinstance(model_usage, dict):
        usage["modelUsage"] = {
            str(model): fold(value) for model, value in model_usage.items()
        }
    total = sum(max(0.0, safe_float(usage.get(key))) for key in (
        "inputTokens", "cachedReadTokens", "outputTokens", "reasoningTokens"))
    return usage if total > 0 else None


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
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
    usage_from_updates = 0
    turn_timestamps = []
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        update = obj.get("params", {}).get("update") if isinstance(obj.get("params"), dict) else None
        if not isinstance(update, dict):
            continue
        ts = timestamp(obj.get("timestamp"))
        if update.get("sessionUpdate") != "turn_completed":
            continue
        if ts is not None:
            turn_timestamps.append(ts)
        usage = update.get("usage")
        model_usage = usage.get("modelUsage") if isinstance(usage, dict) else None
        before = len(events)
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
        if len(events) > before:
            usage_from_updates += 1

    # Grok 1.0 moved token accounting into usage.json. Prefer the ACP stream
    # when it already contains usage so stores present in both formats are not
    # counted twice.
    if usage_from_updates == 0:
        ledger = read_json(Path(session_path) / "usage.json", {})
        if isinstance(ledger, dict):
            turns = ledger.get("turns") if isinstance(ledger.get("turns"), list) else []
            session_totals = ledger.get("session") if isinstance(ledger.get("session"), dict) else None
            records = turns if turns else ([session_totals] if session_totals else [])
            session_timestamp = timestamp(
                summary.get("updated_at") or summary.get("last_active_at")
                or summary.get("created_at"))
            for index, record in enumerate(records):
                usage = _ledger_usage(record)
                if not usage:
                    continue
                ts = turn_timestamps[index] if index < len(turn_timestamps) else session_timestamp
                if ts is None:
                    continue
                model_usage = usage.get("modelUsage")
                if isinstance(model_usage, dict) and model_usage:
                    for model, values in model_usage.items():
                        event = _usage_event(
                            item, f"ledger:{index}:{model}", model, project, ts,
                            values, item.context.get("session_id") or item.path.parent.name,
                        )
                        if event:
                            events.append(event)
                else:
                    event = _usage_event(
                        item, f"ledger:{index}", fallback_model, project, ts, usage,
                        item.context.get("session_id") or item.path.parent.name,
                    )
                    if event:
                        events.append(event)

    warnings = []
    if not events:
        signals = read_json(Path(session_path) / "signals.json", {})
        if isinstance(signals, dict) and safe_int(signals.get("turnCount")) > 0 \
                and isinstance(signals.get("modelsUsed"), list) \
                and any(signals.get("modelsUsed")):
            warnings.append(
                "grok: 会话报告有已完成轮次但未读到用量，可能是用量落盘格式已变化。"
            )
    return batch(events, count, warnings=warnings)
