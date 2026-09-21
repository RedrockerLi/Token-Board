"""Factory Droid session adapter."""

import os
import re
from pathlib import Path

from ..common import (
    batch,
    configured_root,
    iter_jsonl,
    make_event,
    project_name,
    read_json,
    source,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "droid"
LABEL = "Droid"
DEFAULT_PATH = Path.home() / ".factory" / "sessions"
_CUSTOM_SLOT_ID = re.compile(r"^custom:(.+)-\[[^]]+\]-\d+$")
_ROUTING_TIER_IDS = {
    "auto", "default", "default-model", "fast", "turbo", "lite",
    "ultimate", "performance", "efficient",
}


def _project_from_slug(value) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    parts = [part for part in text.rsplit("/", 1)[-1].split("-") if part]
    return parts[-1] if parts else "unknown"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = configured_root(software, DEFAULT_PATH)
    root = (Path(os.environ["VIBE_USAGE_DROID_SESSIONS"]).expanduser()
            if "data_root" not in (software.get("config") or {})
            and "path" not in (software.get("config") or {})
            and os.environ.get("VIBE_USAGE_DROID_SESSIONS", "").strip()
            else configured)
    return [source(path, settings=path.with_name(path.stem + ".settings.json"))
            for path in walk_files(root, (".jsonl",)) if not path.name.endswith(".settings.json")]


def _settings_paths() -> list[Path]:
    override = os.environ.get("VIBE_USAGE_DROID_SETTINGS", "").strip()
    if override:
        return [Path(value).expanduser() for value in override.split(os.pathsep) if value]
    if os.environ.get("VIBE_USAGE_DROID_SESSIONS", "").strip():
        return []
    home = Path.home() / ".factory"
    return [home / name for name in ("settings.json", "settings.local.json", "config.json")]


def _custom_model_catalog() -> dict[str, str]:
    catalog = {}
    for path in _settings_paths():
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        for values in (data.get("customModels"), data.get("custom_models")):
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                identity = str(value.get("id") or "").strip()
                model = str(value.get("model") or "").strip()
                if identity and model:
                    catalog[identity] = model
    return catalog


def _resolve_model(raw, catalog: dict[str, str]) -> str:
    identity = str(raw or "").strip()
    if not identity:
        return "unknown"
    resolved = catalog.get(identity) or identity
    match = _CUSTOM_SLOT_ID.match(resolved)
    if match:
        resolved = match.group(1)
    return f"droid-{resolved.lower()}" if resolved.lower() in _ROUTING_TIER_IDS else resolved


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    messages = list(iter_jsonl(item.path))
    first_ts = None
    count = 0
    session_id = item.path.stem
    project = _project_from_slug(item.path.parent.name)
    for line_no, obj in messages:
        count = line_no
        if obj.get("type") == "message" and timestamp(obj.get("timestamp")) and first_ts is None:
            first_ts = timestamp(obj.get("timestamp"))
    settings = read_json(item.context.get("settings"), {})
    usage = settings.get("tokenUsage", {}) if isinstance(settings, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    cache = usage.get("cacheReadTokens", 0)
    cache_creation = usage.get("cacheCreationTokens", 0)
    thinking = usage.get("thinkingTokens", 0)
    event = make_event(
        kind=KIND, source_key=item.state_key, ordinal="summary",
        model=_resolve_model(settings.get("model"), _custom_model_catalog()),
        requested_at=first_ts, input_tokens=max(0, float(usage.get("inputTokens", 0) or 0))
        + max(0, float(cache_creation or 0)),
        output_tokens=max(0, float(usage.get("outputTokens", 0) or 0) - float(thinking or 0)),
        cached_input_tokens=cache, reasoning_output_tokens=thinking,
        project=project, session_id=session_id,
    )
    return batch([event] if event else [], count)
