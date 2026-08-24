"""Claude Code / Claude Desktop Code JSONL adapter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..common import batch, config_value, iter_jsonl, make_event, project_name, safe_int, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "claude-code"
LABEL = "Claude Code"
DEFAULT_PATH = Path.home() / ".claude"


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    override = os.environ.get("VIBE_USAGE_CLAUDE_DIRS", "").strip()
    if override:
        return [Path(value).expanduser() for value in override.split(os.pathsep) if value]
    roots = [Path.home() / ".claude"]
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        roots.append(Path(explicit).expanduser())
    try:
        roots.extend(path for path in Path.home().glob(".claude-*") if path.is_dir())
    except OSError:
        pass
    desktop_override = os.environ.get("VIBE_USAGE_CLAUDE_DESKTOP_DIRS", "").strip()
    if desktop_override:
        desktop_dirs = [Path(value).expanduser()
                        for value in desktop_override.split(os.pathsep) if value]
    elif sys.platform == "darwin":
        desktop_dirs = [Path.home() / "Library" / "Application Support" / "Claude"]
    elif sys.platform == "win32":
        desktop_dirs = [Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"]
    else:
        desktop_dirs = [Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"]
    for desktop_dir in desktop_dirs:
        roots.extend(_desktop_session_roots(desktop_dir / "local-agent-mode-sessions"))
    return list(dict.fromkeys(roots))


def _desktop_session_roots(directory: Path, depth: int = 0) -> list[Path]:
    if depth > 8:
        return []
    try:
        children = list(directory.iterdir())
    except OSError:
        return []
    result = []
    for child in children:
        if not child.is_dir():
            continue
        if child.name == ".claude":
            result.append(child)
            continue
        if child.name in {"rpm", "skills"}:
            continue
        result.extend(_desktop_session_roots(child, depth + 1))
    return result


def _relative_project(path: Path, directory: Path) -> str:
    try:
        relative = path.relative_to(directory)
        first = relative.parts[0] if relative.parts else ""
    except ValueError:
        first = ""
    parts = [part for part in first.split("-") if part]
    return parts[-1] if parts else "unknown"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    for root in _roots(software):
        for directory in ("projects", "transcripts"):
            base = root / directory
            for path in walk_files(base, (".jsonl",)):
                key = (path.stem, directory == "projects")
                try:
                    stat = path.stat()
                except OSError:
                    continue
                current = candidates.get(key)
                if current is None or (stat.st_size, stat.st_mtime_ns) > current[0]:
                    candidates[key] = ((stat.st_size, stat.st_mtime_ns), path, directory, _relative_project(path, base))
    # A project copy is authoritative when both project and transcript copies
    # exist for the same session id.
    selected = {}
    for (_, is_project), (_, path, directory, fallback_project) in candidates.items():
        session = path.stem
        if session not in selected or is_project:
            selected[session] = (path, directory, fallback_project)
    return [source(path, project_file=directory == "projects", fallback_project=fallback_project)
            for path, directory, fallback_project in selected.values()]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    count = 0
    first_project = item.context.get("fallback_project") or "unknown"
    found_cwd = False
    last_model = None
    seen = {}
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        if not found_cwd and obj.get("cwd"):
            first_project = project_name(obj.get("cwd"))
            found_cwd = True
        if obj.get("type") != "assistant" or not isinstance(obj.get("message"), dict):
            continue
        usage = obj["message"].get("usage")
        if not isinstance(usage, dict):
            continue
        ts = timestamp(obj.get("timestamp"))
        call_id = f"{obj.get('message', {}).get('id', '')}\0{obj.get('requestId', '')}".strip("\0") or str(obj.get("uuid") or line_no)
        raw_model = obj["message"].get("model")
        if isinstance(raw_model, str) and raw_model.strip() and raw_model != "<synthetic>":
            last_model = raw_model.strip()
        breakdown = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        cache_creation = max(
            safe_int(usage.get("cache_creation_input_tokens")),
            safe_int(breakdown.get("ephemeral_5m_input_tokens"))
            + safe_int(breakdown.get("ephemeral_1h_input_tokens")),
        )
        event = make_event(
            kind=KIND, source_key="call", ordinal=call_id,
            model=last_model or "claude-unknown",
            requested_at=ts,
            input_tokens=safe_int(usage.get("input_tokens")) + cache_creation,
            output_tokens=usage.get("output_tokens", 0), cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            project=first_project or "unknown", session_id=item.path.stem,
        )
        if event:
            current = seen.get(call_id)
            score = event.prompt_tokens + event.completion_tokens
            if current is None or score > current[0]:
                seen[call_id] = (score, event)
    events.extend(value[1] for value in seen.values())
    return batch(events, count)
