"""Qwen Code (Gemini CLI fork) adapter."""

from pathlib import Path
import os

from ..common import batch, configured_root, make_event, project_name, source, timestamp, walk_files, iter_jsonl
from ..ir import ParseBatch, UsageSource

KIND = "qwen-code"
LABEL = "Qwen Code"
DEFAULT_PATH = Path.home() / ".qwen" / "tmp"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = configured_root(software, Path(os.environ.get("QWEN_TMP_DIR", DEFAULT_PATH)))
    return [source(path) for path in walk_files(root, (".jsonl",)) if path.parent.name == "chats"]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    seen = set()
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        if obj.get("type") != "assistant":
            continue
        usage = obj.get("usageMetadata")
        if not isinstance(usage, dict):
            continue
        uid = obj.get("uuid")
        if uid and uid in seen:
            continue
        if uid:
            seen.add(uid)
        cached = usage.get("cachedContentTokenCount", 0)
        reasoning = usage.get("thoughtsTokenCount", 0)
        event = make_event(
            kind=KIND, source_key=f"message:{uid}" if uid else item.state_key, ordinal=uid or line_no,
            model=obj.get("model", "unknown"), requested_at=timestamp(obj.get("timestamp")),
            input_tokens=max(0, float(usage.get("promptTokenCount", 0) or 0) - float(cached or 0)),
            output_tokens=max(0, float(usage.get("candidatesTokenCount", 0) or 0) - float(reasoning or 0)),
            cached_input_tokens=cached, reasoning_output_tokens=reasoning,
            project=project_name(obj.get("cwd") or item.path.parent.parent.name), session_id=item.path.stem,
        )
        if event:
            events.append(event)
    return batch(events, count)
