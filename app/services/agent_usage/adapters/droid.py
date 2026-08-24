"""Factory Droid session adapter."""

from pathlib import Path

from ..common import batch, configured_root, iter_jsonl, make_event, project_name, read_json, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "droid"
LABEL = "Droid"
DEFAULT_PATH = Path.home() / ".factory" / "sessions"


def _project_from_slug(value) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    parts = [part for part in text.rsplit("/", 1)[-1].split("-") if part]
    return parts[-1] if parts else "unknown"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = configured_root(software, DEFAULT_PATH)
    return [source(path, settings=path.with_name(path.stem + ".settings.json"))
            for path in walk_files(root, (".jsonl",)) if not path.name.endswith(".settings.json")]


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
    thinking = usage.get("thinkingTokens", 0)
    event = make_event(
        kind=KIND, source_key=item.state_key, ordinal="summary", model=settings.get("model", "unknown"),
        requested_at=first_ts, input_tokens=max(0, float(usage.get("inputTokens", 0) or 0) - float(cache or 0)),
        output_tokens=max(0, float(usage.get("outputTokens", 0) or 0) - float(thinking or 0)),
        cached_input_tokens=cache, reasoning_output_tokens=thinking,
        project=project, session_id=session_id,
    )
    return batch([event] if event else [], count)
