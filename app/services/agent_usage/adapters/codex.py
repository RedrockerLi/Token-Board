"""Codex CLI rollout adapter.

Codex is the one adapter with lineage-aware replay handling.  The parser keeps
the existing duplicate/cumulative semantics while emitting ``UsageEvent``
objects instead of raw dictionaries.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    make_event,
    project_name,
    safe_int,
    source,
    timestamp,
)
from ..ir import ParseBatch, UsageSource
from ..cindy_ledger import discover as discover_cindy, parse as parse_cindy
from app.services.codex_replay import replay_skip_counts

KIND = "codex"
LABEL = "Codex"
DESCRIPTION = "Codex CLI 会话用量"
DEFAULT_PATH_DISPLAY = "~/.codex"
CODEX_HOME = Path.home() / ".codex"
DEFAULT_PATH = CODEX_HOME
SERVICE_TIER_ATTRIBUTION_START = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _service_tier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in {"fast", "priority", "flex", "batch"} else None


def _decorate_model(model: str, service_tier: str | None,
                    requested_at: str | None) -> str:
    if not service_tier or not requested_at or model == "unknown":
        return model
    try:
        parsed = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return model
    if parsed < SERVICE_TIER_ATTRIBUTION_START:
        return model
    return f"{model}-{service_tier}"


def _is_codex_home(path: Path) -> bool:
    return path.is_dir() and any(
        (path / name).is_dir() for name in ("sessions", "archived_sessions")
    )


def _discover_codex_homes(root: Path, max_depth: int = 3) -> list[Path]:
    """Resolve a Codex home or bounded Multica container into Codex homes."""
    root = root.expanduser()
    if _is_codex_home(root):
        return [root]
    if not root.is_dir():
        return []
    homes = []
    queue = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
            except OSError:
                continue
            child_depth = depth + 1
            if child.name == "codex-home" and _is_codex_home(child):
                homes.append(child)
                continue
            if child_depth < max_depth:
                queue.append((child, child_depth))
    result = []
    seen = set()
    for home in homes:
        try:
            key = home.resolve()
        except OSError:
            key = home
        if key not in seen:
            seen.add(key)
            result.append(home)
    return result


def _homes_for_root(root: Path) -> list[Path]:
    root = root.expanduser()
    if root.name in {"sessions", "archived_sessions"}:
        return [root.parent]
    return _discover_codex_homes(root)


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        root = Path(configured).expanduser()
        homes = [root.parent if root.name in {"sessions", "archived_sessions"}
                 else root]
    else:
        homes = [Path(os.environ.get("CODEX_HOME", str(CODEX_HOME))).expanduser()]
    extra = config_value(
        software, "codex_extra_home", "extra_codex_home", "extra_data_root",
    ) or os.environ.get("VIBE_USAGE_CODEX_EXTRA_HOME") or os.environ.get("CODEX_EXTRA_HOME")
    if extra:
        homes.extend(_homes_for_root(Path(extra)))
    for root in configured_extra_roots(software, KIND):
        homes.extend(_homes_for_root(root))
    unique_homes = []
    seen = set()
    for home in homes:
        try:
            key = home.resolve()
        except OSError:
            key = home
        if key not in seen:
            seen.add(key)
            unique_homes.append(home)
    return [home / directory for home in unique_homes
            for directory in ("sessions", "archived_sessions")]


def _session_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", name)
    return match.group(1) if match else path.stem


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    seen = set()
    for root in _roots(software):
        if not root.is_dir():
            continue
        for pattern in ("*.jsonl", "*.jsonl.gz"):
            try:
                paths = root.rglob(pattern)
            except OSError:
                continue
            for path in paths:
                if stop_event is not None and stop_event.is_set():
                    return [source(path, session_id=session_id)
                            for session_id, (_, path) in candidates.items()]
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if path.is_file() and key not in seen:
                    seen.add(key)
                    session_id = _session_id_from_path(path)
                    try:
                        stat = path.stat()
                        score = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        score = (0, 0)
                    current = candidates.get(session_id)
                    if current is None or score > current[0]:
                        candidates[session_id] = (score, path)
    out = [source(path, session_id=session_id)
           for session_id, (_, path) in candidates.items()]
    out.extend(discover_cindy(software, KIND))
    return out


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.name.endswith(".gz") else path.open("r", encoding="utf-8")


def parse(item: UsageSource, stop_event=None, *, skip_token_count: int = 0, **_) -> ParseBatch | None:
    if item.context.get("cindy"):
        return parse_cindy(item, KIND)
    events = []
    session_id = None
    project = None
    model = "codex"
    service_tier = None
    previous_cumulative = None
    raw_token_seen = 0
    line_count = 0
    skip_token_count = max(0, int(skip_token_count or 0))
    try:
        stream = _open(item.path)
        with stream:
            for raw in stream:
                if stop_event is not None and stop_event.is_set():
                    return None
                line_count += 1
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                if obj.get("type") == "session_meta":
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    project = payload.get("project") or payload.get("cwd") or payload.get("workdir") or project
                    if not project and isinstance(payload.get("git"), dict):
                        project = payload["git"].get("repository_url")
                elif obj.get("type") == "turn_context":
                    if isinstance(payload.get("model"), str) and payload["model"].strip():
                        model = payload["model"].strip()
                    if "service_tier" in payload:
                        service_tier = _service_tier(payload.get("service_tier"))
                    project = payload.get("project") or payload.get("cwd") or payload.get("workdir") or project
                elif obj.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                    settings = payload.get("thread_settings")
                    if isinstance(settings, dict):
                        if isinstance(settings.get("model"), str) and settings["model"].strip():
                            model = settings["model"].strip()
                        if "service_tier" in settings:
                            service_tier = _service_tier(settings.get("service_tier"))
                if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                raw_token_seen += 1
                info = payload.get("info") or {}
                if not isinstance(info, dict):
                    continue
                if isinstance(info.get("model") or payload.get("model"), str):
                    model_value = str(info.get("model") or payload.get("model")).strip()
                    if model_value:
                        model = model_value
                cumulative = info.get("total_token_usage")
                cumulative_total = cumulative.get("total_tokens") if isinstance(cumulative, dict) else None
                previous_total = previous_cumulative.get("total_tokens") if isinstance(previous_cumulative, dict) else None
                duplicate = isinstance(cumulative_total, (int, float)) and cumulative_total > 0 and cumulative_total == previous_total
                if duplicate:
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict) and isinstance(cumulative, dict):
                    if isinstance(previous_cumulative, dict):
                        delta = {
                            key: int(cumulative.get(key) or 0) - int(previous_cumulative.get(key) or 0)
                            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
                        }
                        # Counters reset after compaction/new turns in some
                        # Codex builds.  A negative delta is not a negative
                        # usage event; treat the current cumulative snapshot
                        # as a fresh baseline instead.
                        usage = cumulative if any(value < 0 for value in delta.values()) else delta
                    else:
                        usage = cumulative
                if isinstance(cumulative, dict):
                    previous_cumulative = dict(cumulative)
                if not isinstance(usage, dict) or raw_token_seen <= skip_token_count:
                    continue
                event = make_event(
                    kind=KIND,
                    source_key=f"session:{session_id or _session_id_from_path(item.path)}",
                    ordinal=line_count,
                    model=_decorate_model(model, service_tier,
                                           timestamp(obj.get("timestamp"))),
                    requested_at=timestamp(obj.get("timestamp")),
                    input_tokens=usage.get("input_tokens", 0),
                    # Codex's output_tokens includes reasoning_output_tokens;
                    # keep the IR's output bucket exclusive and let
                    # UsageEvent fold reasoning back into completion_tokens.
                    output_tokens=max(0, safe_int(usage.get("output_tokens", 0))
                                      - safe_int(usage.get("reasoning_output_tokens", 0))),
                    cached_input_tokens=usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0)),
                    reasoning_output_tokens=usage.get("reasoning_output_tokens", 0),
                    total_tokens=usage.get("total_tokens"),
                    project=project_name(project), session_id=str(session_id or _session_id_from_path(item.path)),
                    input_includes_cache=True,
                )
                if event:
                    events.append(event)
    except (OSError, EOFError, gzip.BadGzipFile):
        return batch(
            events, line_count, skipped=True,
            warnings=("codex: 会话文件读取不完整，已保留上次同步状态。",),
        )
    return batch(events, line_count)


def replay_skips(sources: list[UsageSource], stop_event=None) -> dict[str, int]:
    # Cindy ledger databases are merged into this adapter's source list, but
    # Codex lineage analysis only understands rollout JSONL files.
    return replay_skip_counts(
        [item.path for item in sources if not item.context.get("cindy")],
        stop_event,
    )
