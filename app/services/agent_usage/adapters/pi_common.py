"""Pi-compatible JSONL session reader shared by Pi-family agents."""

from __future__ import annotations

import os
from pathlib import Path

from ..common import batch, iter_jsonl, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource


def _looks_like_omp_root(path: Path) -> bool:
    """Avoid classifying an OMP data directory as Pi's session store."""
    normalized = str(path).replace("\\", "/").rstrip("/")
    return (
        "/.omp/" in f"{normalized}/"
        or (path / "config.yml").is_file()
        or (path / "agent.db").is_file()
    )


def pi_roots(software: dict, kind: str) -> list[Path]:
    config = software.get("config") or {}
    configured = config.get("data_root") or config.get("path")
    if configured:
        value = Path(str(configured)).expanduser()
        return [value]
    env_name = "VIBE_USAGE_OMP_SESSION_DIRS" if kind == "omp" else "VIBE_USAGE_PI_SESSION_DIRS"
    override = os.environ.get(env_name, "").strip()
    if override:
        return [Path(value).expanduser() for value in override.split(os.pathsep) if value]
    agent_override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if kind != "omp" and agent_override:
        root = Path(agent_override).expanduser()
        if _looks_like_omp_root(root):
            return []
        return [root / "sessions"]
    if kind == "omp":
        config_name = os.environ.get("PI_CONFIG_DIR", ".omp").strip() or ".omp"
        root = Path(config_name).expanduser()
        if not root.is_absolute():
            root = Path.home() / root
        out = [root / "agent" / "sessions"]
        try:
            out.extend(p / "agent" / "sessions" for p in (root / "profiles").iterdir() if p.is_dir())
        except OSError:
            pass
        agent_override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
        if agent_override:
            override_root = Path(agent_override).expanduser()
            if _looks_like_omp_root(override_root):
                out.append(override_root / "sessions")
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            out.append(Path(xdg) / "omp" / "sessions")
            try:
                out.extend(p / "sessions" for p in (Path(xdg) / "omp" / "profiles").iterdir() if p.is_dir())
            except OSError:
                pass
        return out
    return [Path.home() / ".pi" / "agent" / "sessions"]


def discover_pi(software: dict, kind: str) -> list[UsageSource]:
    files = []
    for root in pi_roots(software, kind):
        files.extend((path, root) for path in walk_files(root, (".jsonl",)))
    out = []
    seen = set()
    for path, root in files:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        out.append(source(path, sessions_root=root))
    return out


def _project_from_path(path: Path, sessions_root: Path) -> str:
    """Match Pi's path-only project fallbacks without exposing full paths."""
    try:
        relative = path.relative_to(sessions_root)
        first = relative.parts[0] if relative.parts else "unknown"
    except ValueError:
        first = path.parent.name
    if ".pi-sessions" in path.parts:
        try:
            index = path.parts.index("sessions")
            if index + 1 < len(path.parts):
                return project_name(path.parts[index + 1])
        except ValueError:
            pass
    stripped = first.strip("-")
    if first.startswith("--") and stripped:
        return project_name(stripped.split("-")[-1])
    return project_name(first)


def parse_pi(item: UsageSource, kind: str, stop_event=None) -> ParseBatch:
    events = []
    session_id = item.path.stem
    project = _project_from_path(item.path, Path(item.context.get("sessions_root") or item.path.parent))
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        if stop_event is not None and stop_event.is_set():
            break
        if obj.get("type") == "session":
            session_id = str(obj.get("id") or session_id)
            project = project_name(obj.get("cwd"), project)
            continue
        if obj.get("type") != "message" or not isinstance(obj.get("message"), dict):
            continue
        message = obj["message"]
        role = message.get("role")
        if role != "assistant":
            continue
        ts = timestamp(obj.get("timestamp") or message.get("timestamp"))
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        cache = usage.get("cacheRead", usage.get("cacheReadInputTokens", 0))
        cache_write = usage.get("cacheWrite", usage.get("cacheCreationInputTokens", 0))
        reasoning = usage.get("reasoningTokens", 0)
        record_id = obj.get("id")
        stable_source = f"{session_id}" if record_id is not None else item.state_key
        event = make_event(
            # Copied OMP/Pi session files retain the session/message id.  A
            # stable id lets the request_log UNIQUE constraint collapse those
            # copies even though they have different physical paths.
            kind=kind, source_key=stable_source, ordinal=record_id or line_no,
            model=message.get("model") or message.get("modelId") or obj.get("model") or obj.get("modelId") or "unknown", requested_at=ts,
            input_tokens=usage.get("input", usage.get("inputTokens", usage.get("input_tokens", 0))) + cache_write,
            output_tokens=max(0, float(usage.get("output", usage.get("outputTokens", usage.get("output_tokens", 0))) or 0) - float(reasoning or 0)),
            cached_input_tokens=cache, reasoning_output_tokens=reasoning,
            project=project, session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, count)
