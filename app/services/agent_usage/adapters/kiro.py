"""Kiro native sessions, estimates, and server-side credit snapshots."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from ..common import (
    batch,
    config_value,
    iter_jsonl,
    make_event,
    project_name,
    read_json,
    safe_float,
    source,
    sqlite_rows,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "kiro"
LABEL = "Kiro"
DESCRIPTION = "Kiro CLI 会话与 credits 用量"
DEFAULT_PATH = Path.home() / ".kiro"
CHARS_PER_TOKEN = 4
IMAGE_TOKENS = 1600
ESTIMATE_MODEL = "kiro-token-estimate"
CREDIT_MODEL = "kiro-credits"
NON_TEXT_KEYS = {
    "signature", "redactedContent", "toolUseId", "modelId", "message_id",
    "format", "id",
}
# Kiro has multiple live stores (native streams, SQLite WAL, rotated logs).
# Parsing a source again is cheap compared with silently missing a new live
# record, and request_log.event_id keeps the pass idempotent.
ALWAYS_SCAN = True


def _root(software: dict) -> Path:
    configured = config_value(software, "data_root", "path")
    return Path(configured).expanduser() if configured else DEFAULT_PATH


def _native_dir(root: Path) -> Path:
    override = os.environ.get("KIRO_CLI_SESSIONS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if root.name == "cli":
        return root
    return root / "sessions" / "cli"


def _archive_dir(root: Path) -> Path:
    override = os.environ.get("KIRO_SESSIONS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if root.name in {"sessions", ".kiro_sessions"}:
        return root
    return root / "sessions"


def _cli_db(root: Path) -> Path | None:
    override = os.environ.get("KIRO_CLI_DB_PATH", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    if root.suffix.lower() in {".db", ".sqlite", ".sqlite3"} or root.is_file():
        return root if root.is_file() else None
    candidates = [
        root / "data.sqlite3",
        root / "cli" / "data.sqlite3",
        Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _user_path(root: Path) -> Path | None:
    override = os.environ.get("KIRO_USER_PATH", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_dir() else None
    candidates = [
        root / "User" if root.name != "User" else root,
        Path.home() / ".config" / "Kiro" / "User",
        Path.home() / "Library" / "Application Support" / "Kiro" / "User",
    ]
    return next((path for path in candidates if path.is_dir()), None)


def _q_client_logs(root: Path) -> list[Path]:
    user_path = _user_path(root)
    log_roots = []
    if user_path:
        log_roots.append(user_path.parent / "logs")
    log_roots.append(root / "logs")
    out = []
    seen = set()
    for log_root in log_roots:
        for path in walk_files(log_root, ("q-client.log",)):
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key not in seen:
                seen.add(key)
                out.append(path)
        for path in walk_files(log_root, (".log",)):
            if re.match(r"^q-client\.log\.\d+$", path.name) and path not in out:
                out.append(path)
    return sorted(out, key=lambda path: str(path))


def _logs_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.name == "logs":
            return candidate
    return path.parent


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = _root(software)
    if root.is_file():
        if root.suffix == ".jsonl":
            return [source(root, kind="native", session_dir=root.parent)]
        return [source(root, kind="conversation-db")]

    native_dir = _native_dir(root)
    native_files = list(walk_files(native_dir, (".jsonl",)))
    if native_files:
        out = []
        for path in native_files:
            meta = read_json(native_dir / f"{path.stem}.json", {})
            state = meta.get("session_state") if isinstance(meta, dict) else {}
            model_state = state.get("rts_model_state") if isinstance(state, dict) else {}
            model_info = model_state.get("model_info") if isinstance(model_state, dict) else {}
            out.append(source(
                path,
                kind="native",
                session_dir=native_dir,
                cwd=meta.get("cwd") if isinstance(meta, dict) else None,
                model=model_info.get("model_id") if isinstance(model_info, dict) else None,
            ))
        return out

    archive_dir = _archive_dir(root)
    archives = list(walk_files(archive_dir, (".json",)))
    db = _cli_db(root)
    if archives or db:
        out = [source(path, kind="archive") for path in archives]
        if db:
            out.append(source(db, kind="conversation-db"))
        return out

    if os.environ.get("VIBE_USAGE_KIRO_LEGACY_TOKENS") == "1":
        legacy_db = root / "dev_data" / "devdata.sqlite"
        legacy_jsonl = root / "dev_data" / "tokens_generated.jsonl"
        if legacy_db.is_file():
            return [source(legacy_db, kind="legacy-tokens")]
        if legacy_jsonl.is_file():
            return [source(legacy_jsonl, kind="legacy-tokens")]

    logs = _q_client_logs(root)
    if logs:
        # Parse the complete rotated-log set once. A fixed state key gives
        # snapshots stable event ids across rotations.
        return [source(logs[-1], key="credits", kind="credits", logs_root=str(_logs_root(logs[-1])))]
    return []


def _leaf_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_leaf_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_leaf_chars(value) for key, value in value.items()
                   if key not in NON_TEXT_KEYS)
    return 0


def _estimate(value) -> int:
    return _leaf_chars(value) // CHARS_PER_TOKEN


def _native(item: UsageSource) -> ParseBatch:
    events = []
    pending = 0
    cumulative = 0
    current_ts = None
    model = item.context.get("model")
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        content = data.get("content") if isinstance(data.get("content"), list) else []
        if obj.get("kind") == "Prompt":
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            current_ts = timestamp(meta.get("timestamp")) or current_ts
            pending += sum(
                IMAGE_TOKENS if part.get("kind") == "image" else _estimate(part.get("data"))
                for part in content if isinstance(part, dict)
            )
        elif obj.get("kind") == "ToolResults":
            pending += sum(
                _estimate(part.get("data")) for part in content
                if isinstance(part, dict)
            )
        elif obj.get("kind") == "AssistantMessage":
            output = reasoning = signature = 0
            for part in content:
                value = part.get("data") if isinstance(part, dict) else None
                if isinstance(value, dict) and value.get("modelId"):
                    model = value["modelId"]
                if isinstance(part, dict) and part.get("kind") == "thinking" and isinstance(value, dict):
                    reasoning += len(str(value.get("text", ""))) // CHARS_PER_TOKEN
                    # Signatures are not output, but are re-sent as context.
                    signature += len(str(value.get("signature", ""))) // CHARS_PER_TOKEN
                else:
                    output += _estimate(value)
            try:
                fallback_ts = timestamp(item.path.stat().st_mtime)
            except OSError:
                fallback_ts = None
            event = make_event(
                kind=KIND, source_key=item.state_key, ordinal=line_no,
                model=model or ESTIMATE_MODEL,
                requested_at=current_ts or fallback_ts,
                input_tokens=pending, output_tokens=output,
                cached_input_tokens=cumulative,
                reasoning_output_tokens=reasoning,
                project=project_name(item.context.get("cwd") or "unknown"),
                session_id=item.path.stem,
            )
            if event:
                events.append(event)
            cumulative += pending + output + reasoning + signature
            pending = 0
        elif obj.get("kind") == "Compaction":
            cumulative = _estimate(data.get("summary"))
            pending = 0
    return batch(events, count)


def _conversation_records(item: UsageSource) -> list[dict]:
    if item.context.get("kind") == "archive":
        value = read_json(item.path, {})
        return [value] if isinstance(value, dict) else []
    records = []
    for row in sqlite_rows(
        item.path,
        "SELECT conversation_id,key AS cwd,created_at,updated_at,value FROM conversations_v2",
    ):
        try:
            records.append({
                "conversation_id": row["conversation_id"],
                "cwd": row["cwd"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "value": json.loads(row["value"]),
            })
        except (TypeError, ValueError, KeyError):
            continue
    # Older Kiro CLI builds use `conversations` rather than `conversations_v2`.
    for row in sqlite_rows(item.path, "SELECT key AS cwd,value FROM conversations"):
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(value, dict) or not value.get("conversation_id"):
            continue
        turns = value.get("history") if isinstance(value.get("history"), list) else []
        times = [
            turn.get("request_metadata", {}).get("request_start_timestamp_ms")
            for turn in turns if isinstance(turn, dict)
            and isinstance(turn.get("request_metadata"), dict)
        ]
        records.append({
            "conversation_id": value.get("conversation_id"),
            "cwd": row["cwd"],
            "created_at": min(times) if times else 0,
            "updated_at": max(times) if times else 0,
            "value": value,
        })
    return records


def _conversation_entries(item: UsageSource) -> ParseBatch:
    # A conversation can exist in both an archive and SQLite. Keep the newest
    # copy before estimating it; otherwise an archive move doubles usage.
    by_id = {}
    for record in _conversation_records(item):
        conversation_id = str(record.get("conversation_id") or item.path.stem)
        updated = safe_float(record.get("updated_at"))
        current = by_id.get(conversation_id)
        if current is None or updated >= current[0]:
            by_id[conversation_id] = (updated, record)

    events = []
    for conversation_id, (_, record) in by_id.items():
        data = record.get("value")
        if not isinstance(data, dict):
            continue
        turns = data.get("history") if isinstance(data.get("history"), list) else []
        summary = data.get("latest_summary") or []
        cumulative = _estimate(summary)
        previous_assistant = 0
        for index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            meta = turn.get("request_metadata") if isinstance(turn.get("request_metadata"), dict) else {}
            requested_at = timestamp(meta.get("request_start_timestamp_ms"))
            if not requested_at:
                continue
            user_tokens = _estimate(turn.get("user"))
            assistant_tokens = _estimate(turn.get("assistant"))
            chunks = meta.get("time_between_chunks")
            output_tokens = len(chunks) if isinstance(chunks, list) else 0
            cache = cumulative if index > 0 else 0
            event = make_event(
                kind=KIND, source_key="conversation", ordinal=f"{conversation_id}:{index}",
                model=meta.get("model_id") or ESTIMATE_MODEL,
                requested_at=requested_at,
                input_tokens=user_tokens + (previous_assistant if index > 0 else 0),
                output_tokens=output_tokens, cached_input_tokens=cache,
                project=project_name(record.get("cwd") or data.get("cwd")),
                session_id=conversation_id,
            )
            if event:
                events.append(event)
            cumulative += user_tokens + assistant_tokens
            previous_assistant = assistant_tokens
    return batch(events, len(by_id))


_LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) \[[^\]]+\] (\{.*\})$")


def _text_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                yield line_no, line
    except OSError:
        return


def _credit_snapshots(item: UsageSource) -> list[dict]:
    logs_root = Path(str(item.context.get("logs_root") or item.path.parent))
    files = sorted(walk_files(logs_root, ("q-client.log",)))
    for path in walk_files(logs_root, (".log",)):
        if re.match(r"^q-client\.log\.\d+$", path.name) and path not in files:
            files.append(path)
    snapshots = []
    for path in files:
        for _, raw in _text_lines(path):
            match = _LOG_RE.match(raw.strip())
            if not match:
                continue
            try:
                obj = json.loads(match.group(2))
            except (ValueError, TypeError):
                continue
            if obj.get("commandName") != "GetUsageLimitsCommand":
                continue
            output = obj.get("output") if isinstance(obj.get("output"), dict) else {}
            breakdowns = output.get("usageBreakdownList") or output.get("usageBreakdowns")
            if not isinstance(breakdowns, list):
                continue
            for breakdown in breakdowns:
                if not isinstance(breakdown, dict):
                    continue
                if str(breakdown.get("resourceType") or breakdown.get("type") or "").upper() != "CREDIT":
                    continue
                if str(breakdown.get("unit") or "").upper() != "INVOCATIONS":
                    continue
                free_trial = breakdown.get("freeTrialInfo") if isinstance(breakdown.get("freeTrialInfo"), dict) else {}
                values = [
                    breakdown.get("currentUsageWithPrecision"),
                    breakdown.get("currentUsage"),
                    free_trial.get("currentUsageWithPrecision"),
                    free_trial.get("currentUsage"),
                ]
                current = max((safe_float(value) for value in values), default=0.0)
                reset = str(breakdown.get("nextDateReset") or breakdown.get("resetDate") or "")
                requested_at = timestamp(match.group(1))
                if requested_at is not None:
                    snapshots.append({"timestamp": requested_at, "current": current, "reset": reset})
    return snapshots


def _credits(item: UsageSource) -> ParseBatch:
    unique = {}
    for snapshot in _credit_snapshots(item):
        key = (snapshot["timestamp"], snapshot["reset"], snapshot["current"])
        unique[key] = snapshot
    ordered = sorted(unique.values(), key=lambda value: value["timestamp"])
    events = []
    previous = None
    for snapshot in ordered:
        if previous is None or snapshot["reset"] != previous["reset"] or snapshot["current"] < previous["current"]:
            previous = snapshot
            continue
        delta = math.floor(snapshot["current"]) - math.floor(previous["current"])
        if delta > 0:
            event = make_event(
                kind=KIND, source_key="credits",
                ordinal=f"{snapshot['timestamp']}:{snapshot['reset']}:{snapshot['current']}",
                model=CREDIT_MODEL, requested_at=snapshot["timestamp"],
                output_tokens=delta, project="unknown",
            )
            if event:
                events.append(event)
        previous = snapshot
    return batch(events, len(ordered))


def _legacy_tokens(item: UsageSource) -> ParseBatch:
    if item.path.suffix == ".jsonl":
        rows = []
        for _, obj in iter_jsonl(item.path):
            rows.append({
                "model": obj.get("model") or ESTIMATE_MODEL,
                "prompt": obj.get("promptTokens"),
                "output": obj.get("generatedTokens"),
            })
    else:
        rows = sqlite_rows(item.path, "SELECT id,model,tokens_prompt,tokens_generated,timestamp FROM tokens_generated ORDER BY id")
    events = []
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            model = str(row.get("model") or ESTIMATE_MODEL)
            requested_at = timestamp(item.path.stat().st_mtime)
            prompt = row.get("prompt")
            output = row.get("output")
            ordinal = row.get("id", index)
        else:
            model = str(row["model"] or ESTIMATE_MODEL)
            requested_at = timestamp(row["timestamp"])
            prompt = row["tokens_prompt"]
            output = row["tokens_generated"]
            ordinal = row["id"]
        if not model or model.lower() == "agent":
            model = ESTIMATE_MODEL
        event = make_event(
            kind=KIND, source_key="legacy", ordinal=ordinal, model=model,
            requested_at=requested_at, input_tokens=prompt, output_tokens=output,
            project="unknown", session_id=item.path.stem,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    kind = item.context.get("kind")
    if kind == "native":
        return _native(item)
    if kind == "credits":
        return _credits(item)
    if kind == "legacy-tokens":
        return _legacy_tokens(item)
    return _conversation_entries(item)
