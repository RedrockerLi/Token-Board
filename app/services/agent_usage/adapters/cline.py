"""Cline legacy task history and current SDK session adapter."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..common import (
    batch,
    config_value,
    make_event,
    project_name,
    read_json,
    safe_int,
    source,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "cline"
LABEL = "Cline"
DEFAULT_PATH = Path.home() / ".cline"
HOSTS = ("Code", "Cursor", "Windsurf", "VSCodium", "Code - Insiders", "Trae", "Trae CN")
# SDK session artifacts are rewritten by the desktop app independently of the
# legacy task-history state file.
ALWAYS_SCAN = True


class _ClineUnsupported(ValueError):
    """The SDK artifact shape is known but no longer supported."""


def _read_sdk_json(path: Path):
    """Read one SDK artifact while preserving format-vs-I/O failure classes."""
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _unique(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    override = os.environ.get("VIBE_USAGE_CLINE_DIRS", "").strip()
    if override:
        return _unique([Path(value).expanduser()
                        for value in override.split(os.pathsep) if value.strip()])
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return _unique([
        home / ".cline",
        Path(os.environ.get("CLINE_DIR", home / ".cline")).expanduser(),
        Path(os.environ.get("CLINE_DATA_DIR", home / ".cline" / "data")).expanduser(),
        *[base / host / "User" / "globalStorage" / "saoudrizwan.claude-dev"
          for host in HOSTS],
    ])


def _sdk_session_dirs(software: dict | None = None) -> list[Path]:
    override = os.environ.get("VIBE_USAGE_CLINE_DIRS", "").strip()
    if override:
        roots = [Path(value).expanduser() for value in override.split(os.pathsep)
                 if value.strip()]
        candidates = [root / "sessions" for root in roots]
        candidates.extend(root / "data" / "sessions" for root in roots)
    else:
        candidates = [root / "sessions" for root in _roots(software or {})]
        candidates.extend(root / "data" / "sessions"
                          for root in _roots(software or {}))
        explicit = os.environ.get("CLINE_SESSION_DATA_DIR", "").strip()
        if explicit:
            candidates.append(Path(explicit).expanduser())
    return [path for path in _unique(candidates) if path.is_dir()]


def _sdk_artifacts(software: dict | None = None) -> list[Path]:
    artifacts = []
    for sessions_dir in _sdk_session_dirs(software):
        try:
            children = list(sessions_dir.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            try:
                files = [path for path in child.iterdir()
                         if path.is_file() and path.name.endswith(".messages.json")]
            except OSError:
                continue
            if files and (child / f"{child.name}.json").is_file():
                artifacts.extend(files)
    return _unique(artifacts)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    for root in _roots(software):
        history = read_json(root / "state" / "taskHistory.json")
        if not isinstance(history, list):
            continue
        for item in history:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            path = root / "tasks" / str(item["id"]) / "ui_messages.json"
            if path.is_file():
                identity = str(item.get("ulid") or item["id"])
                try:
                    stat = path.stat()
                    score = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
                current = candidates.get(identity)
                if current is None or score > current[0]:
                    candidates[identity] = (score, path, item)
    out = [source(
        path,
        key=f"task:{identity}",
        task_id=str(item["id"]),
        task_identity=identity,
        task=item,
    ) for identity, (_, path, item) in candidates.items()]
    artifacts = _sdk_artifacts(software)
    if artifacts:
        out.append(source(artifacts[0], key="cline-sdk", sdk_paths=artifacts,
                          sdk=True))
    return out


def _parse_legacy(item: UsageSource) -> ParseBatch:
    messages = read_json(item.path)
    if not isinstance(messages, list):
        return batch([], 0)
    task = item.context.get("task") or {}
    project = project_name(task.get("cwdOnTaskInitialization")
                           or task.get("shadowGitConfigWorkTree")
                           or task.get("cwd"))
    fallback_model = str(task.get("modelId") or "cline-unknown")
    events = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        ts = timestamp(message.get("ts"))
        if message.get("type") != "say" or message.get("say") != "api_req_started":
            continue
        try:
            info = json.loads(message.get("text") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(info, dict):
            continue
        event = make_event(
            kind=KIND,
            source_key=f"task:{item.context.get('task_identity') or item.context.get('task_id')}",
            ordinal=index,
            model=info.get("model") or fallback_model,
            requested_at=ts,
            input_tokens=info.get("tokensIn", 0) + info.get("cacheWrites", 0),
            output_tokens=info.get("tokensOut", 0),
            cached_input_tokens=info.get("cacheReads", 0),
            project=project,
            session_id=item.context.get("task_id"),
        )
        if event:
            events.append(event)
    return batch(events, len(messages))


def _sdk_parse(item: UsageSource, stop_event=None) -> ParseBatch:
    records = {}
    warnings = []
    fatal_warnings = []
    record_count = 0
    paths = item.context.get("sdk_paths") or [item.path]
    paths = sorted((Path(path) for path in paths), key=str)

    # Restored/forked SDK artifacts retain message ids and timestamps.  Process
    # the earliest manifest first so a copied record keeps the same logical
    # session attribution as the reference parser, independent of directory
    # name or filesystem traversal order. The main loop still validates and
    # reports every manifest, so this ordering probe is best-effort only.
    def _started_sort_key(messages_path: Path) -> tuple[str, str, str]:
        session_id = messages_path.parent.name
        try:
            manifest = _read_sdk_json(
                messages_path.parent / f"{session_id}.json")
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return ("", session_id, str(messages_path))
        started = timestamp(manifest.get("started_at")) \
            if isinstance(manifest, dict) else None
        return (started or "", session_id, str(messages_path))

    paths.sort(key=_started_sort_key)
    for messages_path in paths:
        session_dir = messages_path.parent
        session_id = session_dir.name
        try:
            manifest = _read_sdk_json(session_dir / f"{session_id}.json")
            if not isinstance(manifest, dict) or type(manifest.get("version")) is not int \
                    or manifest.get("version") != 1 \
                    or manifest.get("session_id") != session_id:
                raise _ClineUnsupported(
                    f"不支持或不一致的会话清单 {session_dir}")
        except _ClineUnsupported as exc:
            fatal_warnings.append(f"cline: {exc}")
            continue
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            warnings.append(f"cline: 无法读取会话清单 {session_dir}: {exc}")
            continue
        try:
            payload = _read_sdk_json(messages_path)
            origin = payload.get("origin") if isinstance(payload, dict) \
                and isinstance(payload.get("origin"), dict) else {}
            if not isinstance(payload, dict) or type(payload.get("version")) is not int \
                    or payload.get("version") != 1 \
                    or not isinstance(payload.get("messages"), list) \
                    or not isinstance(payload.get("sessionId"), str) \
                    or (payload.get("sessionId") != session_id
                        and origin.get("parentThreadId") != session_id):
                raise _ClineUnsupported(
                    f"不支持或不一致的会话文件 {messages_path}")
        except _ClineUnsupported as exc:
            fatal_warnings.append(f"cline: {exc}")
            continue
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            warnings.append(f"cline: 无法读取会话 {messages_path}: {exc}")
            continue
        project = project_name(manifest.get("workspace_root") or manifest.get("cwd"))
        started = timestamp(manifest.get("started_at")) or ""
        record_count += len(payload["messages"])
        for index, message in enumerate(payload["messages"]):
            if stop_event is not None and stop_event.is_set():
                return batch([], record_count, skipped=True, warnings=warnings)
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            if message.get("role") == "user" and (
                    payload.get("agent") != "lead" or metadata.get("kind")
                    or metadata.get("userRunSpan") == 0
                    or metadata.get("displayRole") in {"system", "status", "error", "tool"}
                    or any(isinstance(block, dict) and block.get("type")
                           in {"tool_result", "tool-result"} for block in content)):
                continue
            raw_ts = message.get("ts")
            if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)):
                continue
            ts = timestamp(raw_ts)
            if ts is None:
                continue
            metric = message.get("metrics") if message.get("role") == "assistant" \
                and isinstance(message.get("metrics"), dict) else {}
            input_total = safe_int(metric.get("inputTokens"))
            cache = min(input_total, safe_int(metric.get("cacheReadTokens")))
            output = safe_int(metric.get("outputTokens"))
            model_info = message.get("modelInfo") if isinstance(message.get("modelInfo"), dict) else {}
            model = model_info.get("id") or manifest.get("model") or "cline-unknown"
            identity = message.get("id") or f"{payload['sessionId']}:{index}"
            key = (str(identity), message.get("role"), ts)
            record = {
                "model": model,
                "input": input_total - cache,
                "output": output,
                "cache": cache,
                "project": project,
                "session_id": session_id,
                "started": started,
                "requested_at": ts,
            }
            old = records.get(key)
            score = record["input"] + record["output"] + record["cache"]
            old_score = (old["input"] + old["output"] + old["cache"]
                         if old else -1)
            if old is None or score > old_score:
                if old is not None:
                    record["session_id"] = old["session_id"]
                    record["project"] = old["project"]
                records[key] = record

    if fatal_warnings:
        return batch([], record_count, skipped=True,
                     warnings=warnings + fatal_warnings)
    events = []
    for index, record in enumerate(records.values()):
        event = make_event(
            kind=KIND, source_key="sdk", ordinal=f"{record['session_id']}:{index}",
            model=record["model"], requested_at=record["requested_at"],
            input_tokens=record["input"], output_tokens=record["output"],
            cached_input_tokens=record["cache"], project=record["project"],
            session_id=record["session_id"],
        )
        if event:
            events.append(event)
    return batch(events, record_count, warnings=warnings)


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    if item.context.get("sdk"):
        return _sdk_parse(item, stop_event)
    return _parse_legacy(item)
