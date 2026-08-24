"""Trae CLI trace/span adapter."""

import os
import sys
from pathlib import Path

from ..common import batch, config_value, iter_jsonl, make_event, project_name, read_json, source, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "trae-cli"
LABEL = "Trae CLI"
if sys.platform == "darwin":
    DEFAULT_PATH = Path.home() / "Library" / "Caches" / "trae-cli" / "sessions"
elif sys.platform == "win32":
    DEFAULT_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "trae-cli" / "cache" / "sessions"
else:
    DEFAULT_PATH = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "trae-cli" / "sessions"
PRIMARY = "model.stream.eino"
FAILOVER = "model.generate"
FALLBACK = ("model.real_call", "model.call")


def _root(software: dict) -> Path:
    configured = config_value(software, "data_root", "path") or os.environ.get("VIBE_USAGE_TRAE_CLI_SESSIONS")
    return Path(configured).expanduser() if configured else DEFAULT_PATH


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = _root(software)
    out = []
    try:
        for session in root.iterdir():
            if session.is_dir() and (session / "traces.jsonl").is_file():
                out.append(source(session / "traces.jsonl", session_path=session, session_id=session.name))
    except OSError:
        pass
    return out


def _tag_map(tags):
    return {str(item.get("key")): item.get("value") for item in tags if isinstance(item, dict) and item.get("key")} if isinstance(tags, list) else {}


def _span_usage(tags):
    return {
        "input": max(0, float(tags.get("usage.input_tokens", 0) or 0)),
        "output": max(0, float(tags.get("usage.output_tokens", 0) or 0)),
        "cache": max(0, float(tags.get("usage.cache_read_tokens", 0) or 0)),
        "reasoning": max(0, float(tags.get("usage.reasoning_tokens", 0) or 0)),
    }


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    spans = []
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        tags = _tag_map(obj.get("tags"))
        usage = _span_usage(tags)
        if sum(usage.values()) <= 0:
            continue
        try:
            start = float(obj.get("startTime"))
        except (TypeError, ValueError):
            continue
        spans.append({"line": line_no, "category": tags.get("span.category", ""), "model": tags.get("model.name") or tags.get("semantic.name"), "start": start, "usage": usage})
    primary = [item for item in spans if item["category"] == PRIMARY]
    failover = [item for item in spans if item["category"] == FAILOVER]
    if primary or failover:
        selected = primary + failover
    else:
        selected = spans
        for category in FALLBACK:
            candidate = [item for item in spans if item["category"] == category]
            if candidate:
                selected = candidate
                break
    session_path = item.context.get("session_path") or item.path.parent
    session = read_json(Path(session_path) / "session.json", {})
    metadata = session.get("metadata", {}) if isinstance(session, dict) else {}
    project = project_name(metadata.get("cwd"))
    fallback_model = metadata.get("model_name") or "trae-unknown"
    events = []
    for span in selected:
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=span["line"], model=span["model"] or fallback_model,
            requested_at=timestamp(span["start"] / 1000), input_tokens=span["usage"]["input"], output_tokens=span["usage"]["output"],
            cached_input_tokens=span["usage"]["cache"], reasoning_output_tokens=span["usage"]["reasoning"], project=project,
            session_id=item.context.get("session_id"),
        )
        if event:
            events.append(event)
    return batch(events, count)
