"""Gemini CLI JSON/JSONL session adapter."""

from pathlib import Path

from ..common import batch, configured_root, iter_jsonl, make_event, project_name, read_json, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "gemini-cli"
LABEL = "Gemini CLI"
DEFAULT_PATH = Path.home() / ".gemini" / "tmp"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return [source(path) for path in walk_files(configured_root(software, DEFAULT_PATH), (".json", ".jsonl"))]


def _records(path: Path):
    if path.suffix == ".jsonl":
        records = []
        directories = None
        for _, obj in iter_jsonl(path):
            if directories is None and isinstance(obj.get("directories"), list):
                directories = obj["directories"]
            if isinstance(obj.get("type") or obj.get("role"), str):
                records.append((obj, directories))
        return records, directories
    data = read_json(path)
    if not isinstance(data, dict):
        return [], None
    return [(obj, data.get("directories")) for obj in (data.get("messages") or data.get("history") or []) if isinstance(obj, dict)], data.get("directories")


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    records, directories = _records(item.path)
    project = project_name((directories or [None])[0]) if directories else "unknown"
    events = []
    for index, (obj, _) in enumerate(records):
        role = obj.get("type") or obj.get("role")
        if role not in {"gemini", "model", "assistant"}:
            continue
        usage = obj.get("tokens") or obj.get("usageMetadata") or obj.get("usage")
        if not isinstance(usage, dict):
            continue
        if "tokens" in obj:
            cached = usage.get("cached", 0)
            reasoning = usage.get("thoughts", 0)
            input_tokens = max(0, float(usage.get("input", 0) or 0) - float(cached or 0))
            output_tokens = max(0, float(usage.get("output", 0) or 0) - float(reasoning or 0))
        else:
            cached = usage.get("cachedContentTokenCount", 0)
            reasoning = usage.get("thoughtsTokenCount", 0)
            input_tokens = max(0, float(usage.get("promptTokenCount", usage.get("input_tokens", 0)) or 0) - float(cached or 0))
            output_tokens = max(0, float(usage.get("candidatesTokenCount", usage.get("output_tokens", 0)) or 0) - float(reasoning or 0))
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=index, model=obj.get("model", "unknown"),
            requested_at=timestamp(obj.get("timestamp") or obj.get("createTime")),
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_input_tokens=cached, reasoning_output_tokens=reasoning,
            project=project, session_id=item.path.stem,
        )
        if event:
            events.append(event)
    return batch(events, len(records))
