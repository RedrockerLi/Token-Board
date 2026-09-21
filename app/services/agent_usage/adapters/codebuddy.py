"""CodeBuddy Code CLI usage adapter (Tencent terminal agent)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..common import (
    batch,
    config_value,
    iter_jsonl,
    make_event,
    project_name,
    safe_int,
    source,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "codebuddy"
LABEL = "CodeBuddy"
DESCRIPTION = "CodeBuddy 本地用量"
DEFAULT_PATH = Path.home() / ".codebuddy" / "projects"

ROUTING_TIER_IDS = {
    "auto", "default", "default-model", "fast", "turbo", "lite",
    "ultimate", "performance", "efficient",
}


def normalize_model(model: Any) -> str:
    m = str(model or "").strip()
    if not m:
        return "unknown"
    lower = m.lower()
    return f"codebuddy-{lower}" if lower in ROUTING_TIER_IDS else m


def resolve_roots(software: dict | None = None) -> list[Path]:
    if software:
        override = config_value(software, "data_root", "path")
        if override:
            root = Path(override).expanduser()
            if root.name == "projects" or not (root / "projects").is_dir():
                return [root]
            return [root / "projects"]
    env_dirs = os.environ.get("VIBE_USAGE_CODEBUDDY_DIRS", "").strip()
    if env_dirs:
        return [Path(part.strip()).expanduser() for part in env_dirs.split(os.pathsep) if part.strip()]
    config_dir = os.environ.get("CODEBUDDY_CONFIG_DIR", "").strip()
    if config_dir:
        return [Path(config_dir).expanduser() / "projects"]
    return [DEFAULT_PATH]


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    sources = []
    for root in resolve_roots(software):
        projects_dir = root if root.name == "projects" or not (root / "projects").is_dir() else root / "projects"
        if not projects_dir.is_dir():
            continue
        try:
            for path in walk_files(projects_dir, (".jsonl",)):
                sources.append(source(path, projects_dir=projects_dir))
        except OSError as err:
            sources.append(source(
                projects_dir,
                key=f"{KIND}-discovery",
                discovery_warnings=(f"codebuddy: cannot read directory {projects_dir}: {err}",),
            ))
    return sources


def _extract_fallback_project(file_path: Path, projects_dir: Path) -> str:
    try:
        relative = file_path.relative_to(projects_dir)
        folder = relative.parts[0] if relative.parts else ""
    except ValueError:
        folder = file_path.parent.name
    parts = [part for part in folder.split("-") if part]
    return parts[-1] if parts else "unknown"


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    if not item.path.is_file():
        return batch([], 0)

    projects_dir = Path(item.context.get("projects_dir") or item.path.parent.parent)
    fallback_project = _extract_fallback_project(item.path, projects_dir)
    session_id = item.path.stem

    by_key: dict[str, dict] = {}
    anonymous: list[dict] = []
    count = 0

    try:
        for line_no, obj in iter_jsonl(item.path):
            count = line_no
            if stop_event is not None and stop_event.is_set():
                break
            if not isinstance(obj, dict):
                continue

            message = obj.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if not isinstance(usage, dict):
                continue

            raw_ts = obj.get("timestamp") or (message.get("timestamp") if isinstance(message, dict) else None)
            ts = timestamp(raw_ts)
            if not ts:
                continue

            cache_write = safe_int(usage.get("cache_creation_input_tokens"))
            input_tokens = safe_int(usage.get("input_tokens")) + cache_write
            output_tokens = safe_int(usage.get("output_tokens"))
            cached_input_tokens = safe_int(usage.get("cache_read_input_tokens"))
            if input_tokens + output_tokens + cached_input_tokens == 0:
                continue

            provider_data = obj.get("providerData") if isinstance(obj.get("providerData"), dict) else {}
            msg_id = ""
            if isinstance(message, dict) and message.get("id"):
                msg_id = str(message.get("id")).strip()
            if not msg_id and provider_data.get("messageId"):
                msg_id = str(provider_data.get("messageId")).strip()
            if not msg_id and obj.get("id"):
                msg_id = str(obj.get("id")).strip()

            dedup_key = f"call:{msg_id}" if msg_id else None

            # Model resolution
            candidates = [
                message.get("model") if isinstance(message, dict) else None,
                provider_data.get("requestModelId"),
                provider_data.get("model"),
            ]
            raw_model = next((str(c).strip() for c in candidates if isinstance(c, str) and str(c).strip()), "unknown")
            model = normalize_model(raw_model)

            cwd = obj.get("cwd")
            project = project_name(cwd, fallback_project) if cwd else fallback_project
            s_id = str(obj.get("sessionId") or session_id)

            record_entry = {
                "dedup_key": dedup_key,
                "ordinal": dedup_key or f"line:{line_no}",
                "usage_score": input_tokens + output_tokens + cached_input_tokens,
                "model": model,
                "project": project,
                "requested_at": ts,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "session_id": s_id,
            }

            if not dedup_key:
                anonymous.append(record_entry)
            else:
                curr = by_key.get(dedup_key)
                if not curr or record_entry["usage_score"] > curr["usage_score"]:
                    by_key[dedup_key] = record_entry

    except OSError as err:
        return batch([], 0, skipped=True, warnings=(f"codebuddy: cannot read {item.path}: {err}",))

    events = []
    for entry in [*anonymous, *by_key.values()]:
        event = make_event(
            kind=KIND,
            source_key=item.state_key,
            ordinal=entry["ordinal"],
            model=entry["model"],
            requested_at=entry["requested_at"],
            input_tokens=entry["input_tokens"],
            output_tokens=entry["output_tokens"],
            cached_input_tokens=entry["cached_input_tokens"],
            project=entry["project"],
            session_id=entry["session_id"],
        )
        if event:
            events.append(event)

    return batch(events, count)
