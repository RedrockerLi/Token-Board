"""Pi-compatible JSONL session reader shared by Pi-family agents."""

from __future__ import annotations

import os
import logging
import json
from pathlib import Path

from ..common import (
    batch,
    configured_extra_roots,
    iter_jsonl,
    make_event,
    project_name,
    read_json,
    safe_float,
    source,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

log = logging.getLogger(__name__)

_PI_SESSIONS_FOUND = "found"
_PI_SESSIONS_ABSENT = "absent"
_PI_SESSIONS_UNREADABLE = "unreadable"
_PI_MESSAGE_ROLES = {"user", "assistant", "toolResult"}


def _is_non_empty_string(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_pi_session_record(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") == "session":
        return (
            value.get("version") is not None
            and _is_non_empty_string(value.get("id"))
            and _is_non_empty_string(value.get("timestamp"))
            and isinstance(value.get("cwd"), str)
        )
    if value.get("type") != "message" or not isinstance(value.get("message"), dict):
        return False
    return (
        _is_non_empty_string(value.get("id"))
        and "parentId" in value
        and _is_non_empty_string(value.get("timestamp"))
        and value["message"].get("role") in _PI_MESSAGE_ROLES
    )


def _probe_pi_file(path: Path) -> str:
    try:
        text = path.read_bytes()[:16 * 1024].decode("utf-8", errors="replace")
    except OSError:
        return _PI_SESSIONS_UNREADABLE
    checked = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        checked += 1
        if checked > 10:
            break
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if _is_pi_session_record(value):
            return _PI_SESSIONS_FOUND
    return _PI_SESSIONS_ABSENT


def _probe_pi_sessions(directory: Path, depth: int = 2) -> str:
    try:
        children = list(directory.iterdir())
    except OSError:
        return _PI_SESSIONS_UNREADABLE
    unreadable = False
    for child in children:
        try:
            if child.is_dir():
                continue
            if child.name.endswith(".jsonl"):
                state = _probe_pi_file(child)
                if state == _PI_SESSIONS_FOUND:
                    return state
                if state == _PI_SESSIONS_UNREADABLE:
                    unreadable = True
        except OSError:
            unreadable = True
    if depth > 0:
        for child in children:
            try:
                if not child.is_dir():
                    continue
                state = _probe_pi_sessions(child, depth - 1)
            except OSError:
                state = _PI_SESSIONS_UNREADABLE
            if state == _PI_SESSIONS_FOUND:
                return state
            if state == _PI_SESSIONS_UNREADABLE:
                unreadable = True
    return _PI_SESSIONS_UNREADABLE if unreadable else _PI_SESSIONS_ABSENT


def _probe_pi_outside_sessions(root: Path) -> str:
    try:
        children = list(root.iterdir())
    except OSError:
        return _PI_SESSIONS_UNREADABLE
    unreadable = False
    for child in children:
        if child.name == "sessions":
            continue
        try:
            if not child.is_dir():
                continue
            state = _probe_pi_sessions(child, 1)
        except OSError:
            state = _PI_SESSIONS_UNREADABLE
        if state == _PI_SESSIONS_FOUND:
            return state
        if state == _PI_SESSIONS_UNREADABLE:
            unreadable = True
    return _PI_SESSIONS_UNREADABLE if unreadable else _PI_SESSIONS_ABSENT


def _resolve_pi_sessions_root(value: Path) -> Path | None:
    """Resolve an explicitly configured Pi root from its actual contents.

    A configured path may be a sessions directory, an agent home containing
    ``sessions/``, or a container of task-local stores. Content validation
    keeps an unrelated ``.jsonl`` tree from being imported as an empty Pi
    source, and keeps a newly unreadable sibling from narrowing the scan.
    """
    root = value.expanduser()
    if not root.is_dir():
        return None
    direct = _probe_pi_sessions(root, 0)
    if direct == _PI_SESSIONS_FOUND:
        return root
    outside = _probe_pi_outside_sessions(root)
    if direct == _PI_SESSIONS_UNREADABLE or outside == _PI_SESSIONS_UNREADABLE:
        return None
    nested = root / "sessions"
    if (
        outside == _PI_SESSIONS_ABSENT
        and nested.is_dir()
        and _probe_pi_sessions(nested) == _PI_SESSIONS_FOUND
    ):
        return nested
    return root if _probe_pi_sessions(root) == _PI_SESSIONS_FOUND else None


def _looks_like_omp_root(path: Path) -> bool:
    """Avoid classifying an OMP data directory as Pi's session store."""
    normalized = str(path).replace("\\", "/").rstrip("/")
    return (
        "/.omp/" in f"{normalized}/"
        or (path / "config.yml").is_file()
        or (path / "agent.db").is_file()
    )


def _configured_session_dir(agent_dir: Path) -> Path | None:
    """Read Pi's persistent absolute sessionDir setting when available."""
    settings = read_json(agent_dir / "settings.json", {})
    value = settings.get("sessionDir") if isinstance(settings, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value.strip()).expanduser()
    # Relative values are resolved by Pi against the invoking cwd, which the
    # background importer does not know. Scanning one guessed cwd could import
    # an unrelated store, so only stable absolute settings are discoverable.
    return candidate if candidate.is_absolute() else None


def pi_roots(software: dict, kind: str) -> list[Path]:
    config = software.get("config") or {}
    configured = config.get("data_root") or config.get("path")
    extra = [
        resolved
        for root in configured_extra_roots(software, kind)
        if (resolved := _resolve_pi_sessions_root(root)) is not None
    ]
    if configured:
        value = Path(str(configured)).expanduser()
        return list(dict.fromkeys([value, *extra]))
    env_name = "VIBE_USAGE_OMP_SESSION_DIRS" if kind == "omp" else "VIBE_USAGE_PI_SESSION_DIRS"
    override = os.environ.get(env_name, "").strip()
    if override:
        return list(dict.fromkeys(
            [Path(value).expanduser() for value in override.split(os.pathsep) if value]
            + extra
        ))
    agent_override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if kind != "omp" and agent_override:
        root = Path(agent_override).expanduser()
        if _looks_like_omp_root(root):
            return extra
        roots = [root / "sessions"]
        session_override = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
        if session_override:
            roots.append(Path(session_override).expanduser())
        configured_session = _configured_session_dir(root)
        if configured_session is not None:
            roots.append(configured_session)
        return list(dict.fromkeys([*roots, *extra]))
    if kind == "omp":
        config_name = os.environ.get("PI_CONFIG_DIR", ".omp").strip() or ".omp"
        root = Path(config_name).expanduser()
        if not root.is_absolute():
            root = Path.home() / root
        out = [root / "agent" / "sessions"]
        try:
            out.extend(p / "agent" / "sessions" for p in (root / "profiles").iterdir() if p.is_dir())
        except OSError:
            log.debug("Pi profile root is unavailable", exc_info=True)
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
                log.debug("OMP profile root is unavailable", exc_info=True)
        return list(dict.fromkeys([*out, *extra]))
    agent_dir = Path.home() / ".pi" / "agent"
    roots = [agent_dir / "sessions"]
    session_override = os.environ.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if session_override:
        roots.append(Path(session_override).expanduser())
    configured_session = _configured_session_dir(agent_dir)
    if configured_session is not None:
        roots.append(configured_session)
    return list(dict.fromkeys([*roots, *extra]))


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
            log.debug("Pi session path is outside its expected root", exc_info=True)
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
        # Pi's current Usage shape calls this subset `reasoning`; older
        # session writers used `reasoningTokens`.  Both are included for
        # compatibility, while output is kept exclusive of reasoning.
        reasoning = usage.get("reasoning", usage.get("reasoningTokens", 0))
        record_id = obj.get("id")
        stable_source = f"{session_id}" if record_id is not None else item.state_key
        event = make_event(
            # Copied OMP/Pi session files retain the session/message id.  A
            # stable id lets the request_log UNIQUE constraint collapse those
            # copies even though they have different physical paths.
            kind=kind, source_key=stable_source,
            ordinal=record_id if record_id is not None else line_no,
            model=message.get("model") or message.get("modelId") or obj.get("model") or obj.get("modelId") or "unknown", requested_at=ts,
            input_tokens=(
                safe_float(usage.get("input", usage.get("inputTokens", usage.get("input_tokens", 0))))
                + safe_float(cache_write)
            ),
            output_tokens=max(
                0,
                safe_float(usage.get("output", usage.get("outputTokens", usage.get("output_tokens", 0))))
                - safe_float(reasoning),
            ),
            cached_input_tokens=safe_float(cache),
            reasoning_output_tokens=safe_float(reasoning),
            total_tokens=(
                safe_float(usage.get("input", usage.get("inputTokens", usage.get("input_tokens", 0))))
                + safe_float(cache_write)
                + max(
                    0,
                    safe_float(usage.get("output", usage.get("outputTokens", usage.get("output_tokens", 0))))
                    - safe_float(reasoning),
                )
                + safe_float(reasoning)
            ),
            project=project, session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, count)
