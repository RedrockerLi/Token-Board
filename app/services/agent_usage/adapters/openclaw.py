"""OpenClaw profile session adapter."""

import os
import logging
from pathlib import Path

from ..common import batch, config_value, iter_jsonl, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "openclaw"
LABEL = "OpenClaw"
DEFAULT_PATH = Path.home() / ".openclaw" / "agents"
log = logging.getLogger(__name__)


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    override = os.environ.get("VIBE_USAGE_OPENCLAW_DIRS", "").strip()
    if override:
        return [Path(value).expanduser() for value in override.split(os.pathsep) if value]
    home = Path.home()
    out = [home / name for name in (".clawdbot", ".moltbot", ".moldbot", ".openclaw")]
    try:
        out.extend(home / child.name for child in home.iterdir() if child.is_dir() and (child.name == ".openclaw" or child.name.startswith(".openclaw-")))
    except OSError:
        log.debug("OpenClaw home is unavailable", exc_info=True)
    return list(dict.fromkeys(out))


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    out = []
    for root in _roots(software):
        agents = root if root.name == "agents" else root / "agents"
        out.extend(source(path, project=path.parent.parent.name)
                   for path in walk_files(agents, (".jsonl",)))
    return out


def _number(usage: dict, *keys) -> int:
    for key in keys:
        value = usage.get(key)
        try:
            if float(value or 0) > 0:
                return max(0, int(float(value)))
        except (TypeError, ValueError):
            log.debug("OpenClaw usage value is not numeric", exc_info=True)
    return 0


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    project = str(item.context.get("project") or item.path.parent.parent.name or "unknown")
    count = 0
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        if obj.get("type") != "message" or not isinstance(obj.get("message"), dict):
            continue
        message = obj["message"]
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=line_no,
            model=message.get("model") or obj.get("model") or "unknown",
            requested_at=timestamp(obj.get("timestamp") or message.get("timestamp")),
            input_tokens=_number(usage, "input", "inputTokens", "input_tokens", "promptTokens", "prompt_tokens")
            + _number(usage, "cacheCreation", "cacheCreationInputTokens", "cacheWrite", "cache_creation", "cache_write", "cache_creation_input_tokens", "cache_write_input_tokens"),
            output_tokens=_number(usage, "output", "outputTokens", "output_tokens", "completionTokens", "completion_tokens"),
            cached_input_tokens=_number(usage, "cacheRead", "cache_read", "cache_read_input_tokens"),
            project=project, session_id=item.path.stem,
        )
        if event:
            events.append(event)
    return batch(events, count)
