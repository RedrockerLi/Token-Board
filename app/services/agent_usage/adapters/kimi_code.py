"""Kimi Code current and legacy wire-format adapter."""

import errno
import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterator

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    iter_jsonl,
    make_event,
    project_name,
    read_json,
    source,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "kimi-code"
LABEL = "Kimi Code"
DEFAULT_PATH = Path.home() / ".kimi-code"
log = logging.getLogger(__name__)


def _kimi_work_home() -> Path:
    if os.name == "nt":
        user_data = Path(os.environ.get(
            "APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        user_data = Path.home() / "Library" / "Application Support"
    else:
        user_data = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return user_data / "kimi-desktop" / "daimon-share" / "daimon" / "runtime" / "kimi-code" / "home"


def _roots(software: dict) -> tuple[list[Path], Path]:
    configured = (config_value(software, "data_root", "path")
                  or os.environ.get("VIBE_USAGE_KIMI_CODE_DIR"))
    if configured:
        current = [Path(configured).expanduser()]
    else:
        current = [Path(os.environ.get(
            "KIMI_CODE_HOME", Path.home() / ".kimi-code")).expanduser(),
                   _kimi_work_home()]
    current.extend(configured_extra_roots(software, KIND))
    unique = []
    seen = set()
    for root in current:
        try:
            key = root.resolve()
        except OSError:
            key = root
        if key not in seen:
            seen.add(key)
            unique.append(root)
    legacy = Path(os.environ.get(
        "KIMI_USAGE_LEGACY_ROOT", os.environ.get(
            "VIBE_USAGE_KIMI_DIR", Path.home() / ".kimi"))).expanduser()
    return unique, legacy


def _read_failure(path: Path, exc: OSError) -> str:
    """One warning line for a path that exists but cannot be read."""
    code = errno.errorcode.get(exc.errno) if exc.errno is not None else None
    return f"kimi-code: 无法读取 {path}: {code or exc}"


def _is_directory(entry: Path, failures: list[tuple[Path, str]]) -> bool:
    """Classify one directory entry without letting a stat failure escape."""
    try:
        return entry.is_dir()
    except OSError as exc:
        failures.append((entry, _read_failure(entry, exc)))
        return False


def _wire_files(sessions: Path, failures: list[tuple[Path, str]]) -> list[Path]:
    """Every ``wire.jsonl`` under one current-format store, at any depth.

    A missing directory is a normal empty result: Kimi homes are optional and a
    session may legitimately have no agent wire yet.  Any other read failure
    means sessions the adapter would otherwise collect are unreadable, so it is
    recorded and the caller reports it instead of an empty success.
    """
    out = []
    pending = [sessions]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            failures.append((directory, _read_failure(directory, exc)))
            continue
        for entry in entries:
            if _is_directory(entry, failures):
                pending.append(entry)
                continue
            if entry.name != "wire.jsonl":
                continue
            try:
                if entry.is_file():
                    out.append(entry)
            except OSError as exc:
                failures.append((entry, _read_failure(entry, exc)))
    return sorted(out, key=str)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    current_roots, legacy = _roots(software)
    legacy_sessions = legacy if legacy.name == "sessions" else legacy / "sessions"
    out = []
    seen = set()
    failures: list[tuple[Path, str]] = []
    for current in current_roots:
        current_sessions = current if current.name == "sessions" else current / "sessions"
        for path in _wire_files(current_sessions, failures):
            try:
                identity = path.resolve()
            except OSError:
                identity = path
            if identity in seen:
                continue
            seen.add(identity)
            out.append(source(
                path, layout="current", session_dir=path.parents[2], root=current,
            ))
    out.extend(source(path, layout="legacy", work_dir_hash=path.parents[1].name, root=legacy)
               for path in walk_files(legacy_sessions, ("wire.jsonl",)))
    if failures:
        # Unreadable paths get their own warning source so their cursors never
        # advance; the readable sources above still import, like the reference
        # parser, which keeps the buckets it could read and only marks the
        # result skipped.
        out.append(source(
            failures[0][0],
            key="kimi-code-discovery",
            discovery_warnings=tuple(dict.fromkeys(
                message for _, message in failures)),
        ))
    return list(dict.fromkeys(out))


def _load_index(root: Path) -> dict[str, str]:
    out = {}
    for _, row in iter_jsonl(root / "session_index.jsonl"):
        if row.get("sessionDir") and row.get("workDir"):
            value = str(row["sessionDir"])
            out[value] = project_name(row["workDir"])
            try:
                out[str(Path(value).expanduser().resolve())] = project_name(row["workDir"])
            except OSError:
                log.debug("Kimi Code project path is unavailable", exc_info=True)
    return out


def _project_from_bucket(name: str) -> str:
    value = str(name or "unknown")
    match = re.match(r"^wd_(.+)_[0-9a-fA-F]+$", value)
    return project_name(match.group(1) if match else value)


def _legacy_project_map(root: Path) -> dict[str, str]:
    data = read_json(root / "kimi.json", {})
    out = {}
    if not isinstance(data, dict):
        return out
    for entry in data.get("work_dirs", []) if isinstance(data.get("work_dirs"), list) else []:
        if isinstance(entry, dict) and entry.get("path"):
            out[hashlib.md5(str(entry["path"]).encode()).hexdigest()] = project_name(entry["path"])
    for key in ("workspaces", "projects"):
        values = data.get(key)
        if not isinstance(values, dict):
            continue
        for digest, info in values.items():
            if isinstance(info, str):
                path = info
            elif isinstance(info, dict):
                path = info.get("path") or info.get("dir")
            else:
                path = None
            if path:
                out[str(digest)] = project_name(path)
    return out


def _legacy_model(root: Path) -> str:
    try:
        text = (root / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"^\s*default_model\s*=\s*[\"']([^\"']+)", text, re.M)
    if match:
        return match.group(1)
    match = re.search(r"^\s*\[models\.(?:\"([^\"]+)\"|([A-Za-z0-9_-]+))\]", text, re.M)
    return (match.group(1) or match.group(2)) if match else "unknown"


def _wire_records(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield parsed wire records with their one-based physical line number.

    Same contract as ``common.iter_jsonl``, except that read failures reach the
    caller: the shared helper swallows ``OSError``, which would turn an
    existing-but-unreadable wire file into an empty success and let the
    importer advance that file's cursor past records it never read.
    """
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line_no, raw in enumerate(source, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                yield line_no, value


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    layout = item.context.get("layout") or "legacy"
    # /.../.kimi-code/sessions/wd_x/session_y/agents/a/wire.jsonl
    # /.../.kimi/sessions/hash/session_y/wire.jsonl
    if item.context.get("root"):
        root = Path(item.context["root"])
    else:
        try:
            root = item.path.parents[5] if layout == "current" else item.path.parents[3]
        except IndexError:
            return batch([], 0)
    root = Path(root)
    project_map = _load_index(root) if item.context.get("layout") == "current" else _legacy_project_map(root)
    session_dir = item.context.get("session_dir")
    if session_dir:
        project = project_map.get(str(session_dir))
        if project is None:
            try:
                project = project_map.get(str(Path(session_dir).resolve()))
            except OSError:
                project = None
        project = project or project_name(Path(session_dir).parent.name)
    else:
        work_hash = str(item.context.get("work_dir_hash") or "")
        project = project_map.get(work_hash) or _project_from_bucket(work_hash)
    model = _legacy_model(root) if item.context.get("layout") == "legacy" else "unknown"
    events = []
    count = 0
    seen = set()
    last_ts = None
    try:
        for line_no, raw in _wire_records(item.path):
            count = line_no
            if item.context.get("layout") == "current":
                ts = timestamp(raw.get("time"))
                if raw.get("type") == "usage.record":
                    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
                    event = make_event(
                        # Main and sub-agent wires share one logical sessionDir,
                        # but each contains independent usage deltas. Include the
                        # agent directory in the stable key or same-line records
                        # from both wires would collapse in request_log.
                        kind=KIND,
                        source_key=(f"current:{session_dir or item.path.stem}:"
                                    f"{item.path.parent.name}"),
                        ordinal=raw.get("id") or line_no,
                        model=raw.get("model", "unknown"), requested_at=ts,
                        input_tokens=usage.get("inputOther", 0) + usage.get("inputCacheCreation", 0),
                        output_tokens=usage.get("output", 0), cached_input_tokens=usage.get("inputCacheRead", 0),
                        project=project, session_id=str(session_dir or item.path.stem),
                    )
                    if event:
                        events.append(event)
                continue
            envelope = raw.get("message") if isinstance(raw.get("message"), dict) else raw
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict):
                continue
            last_ts = timestamp(raw.get("timestamp") or payload.get("timestamp")) or last_ts
            if payload.get("model"):
                model = payload["model"]
            if envelope.get("type") != "StatusUpdate" or not isinstance(payload.get("token_usage"), dict):
                continue
            message_id = payload.get("message_id")
            if message_id and message_id in seen:
                continue
            if message_id:
                seen.add(message_id)
            usage = payload["token_usage"]
            event = make_event(
                kind=KIND, source_key="legacy", ordinal=message_id or f"{item.path.stem}:{line_no}", model=model,
                requested_at=last_ts, input_tokens=usage.get("input_other", 0) + usage.get("input_cache_creation", 0),
                output_tokens=usage.get("output", 0), cached_input_tokens=usage.get("input_cache_read", 0),
                project=project, session_id=item.path.stem,
            )
            if event:
                events.append(event)
    except OSError as exc:
        # Whatever parsed before the failure is kept, but the batch is marked
        # skipped so the importer neither advances this file's cursor nor
        # treats the unread records as gone.
        return batch(events, count, skipped=True,
                     warnings=(_read_failure(item.path, exc),))
    return batch(events, count)
