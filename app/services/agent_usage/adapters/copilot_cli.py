"""GitHub Copilot CLI session-state adapter."""

import os
from pathlib import Path

from ..common import batch, configured_root, iter_jsonl, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "copilot-cli"
LABEL = "GitHub Copilot CLI"
DEFAULT_PATH = Path.home() / ".copilot" / "session-state"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = configured_root(software, Path(os.environ.get("COPILOT_SESSION_STATE_DIR", DEFAULT_PATH)))
    return [source(path) for path in walk_files(root, ("events.jsonl",))]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    project = "unknown"
    session_id = item.path.parent.name
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        ts = timestamp(obj.get("timestamp"))
        if obj.get("type") in {"session.start", "session.resume"}:
            context = obj.get("data", {}).get("context", {})
            if isinstance(context, dict):
                project = project_name(context.get("gitRoot") or context.get("cwd"))
        if obj.get("type") != "session.shutdown" or not ts:
            continue
        metrics = obj.get("data", {}).get("modelMetrics", {})
        if not isinstance(metrics, dict):
            continue
        for model, metric in metrics.items():
            usage = metric.get("usage", {}) if isinstance(metric, dict) else {}
            if not isinstance(usage, dict):
                continue
            cache = usage.get("cacheReadTokens", 0)
            event = make_event(
                kind=KIND, source_key=item.state_key, ordinal=f"{line_no}:{model}",
                model=model, requested_at=ts,
                input_tokens=max(0, int(float(usage.get("inputTokens", 0) or 0)) - max(0, int(float(cache or 0)))),
                output_tokens=usage.get("outputTokens", 0), cached_input_tokens=cache,
                project=project, session_id=session_id,
            )
            if event:
                events.append(event)
    return batch(events, count)
