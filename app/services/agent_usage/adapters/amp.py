"""Amp thread JSON adapter."""

import logging
import os
import sys
from pathlib import Path

from ..common import batch, config_value, make_event, read_json, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "amp"
LABEL = "Amp"
DEFAULT_PATH = Path.home() / ".local" / "share" / "amp" / "threads"
log = logging.getLogger(__name__)


def _default_root() -> Path:
    """The XDG-style default, or the Windows native store when it exists.

    Amp on Windows keeps its threads under ``%LOCALAPPDATA%\\amp\\threads`` and
    never writes the XDG-style default there, so without this probe the source
    silently collected nothing.  The native store is used only when it already
    exists; the default root stays the fallback, and POSIX hosts never probe it.
    """
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if sys.platform == "win32" and local_app_data:
        native = Path(local_app_data).expanduser() / "amp" / "threads"
        try:
            if native.is_dir():
                return native
        except OSError:
            log.debug("Amp native store is unavailable", exc_info=True)
    return DEFAULT_PATH


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = config_value(software, "data_root", "path")
    if configured:
        root = Path(configured).expanduser()
    elif os.environ.get("AMP_DATA_DIR"):
        root = Path(os.environ["AMP_DATA_DIR"]).expanduser()
    elif os.environ.get("XDG_DATA_HOME"):
        root = Path(os.environ["XDG_DATA_HOME"]).expanduser() / "amp" / "threads"
    else:
        root = _default_root()
    return [source(path) for path in walk_files(root, (".json",)) if path.name.startswith("T-")]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    thread = read_json(item.path)
    if not isinstance(thread, dict):
        return batch([], 0)
    messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    ledger = thread.get("usageLedger", {}).get("events") if isinstance(thread.get("usageLedger"), dict) else []
    ledger = ledger if isinstance(ledger, list) else []
    events = []
    if ledger:
        for index, record in enumerate(ledger):
            to_id = record.get("toMessageId") if isinstance(record, dict) else None
            target = messages[to_id] if isinstance(to_id, int) and 0 <= to_id < len(messages) else {}
            usage = target.get("usage") if isinstance(target, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            tokens = record.get("tokens") if isinstance(record, dict) else {}
            tokens = tokens if isinstance(tokens, dict) else {}
            event = make_event(
                kind=KIND, source_key=item.state_key, ordinal=f"ledger:{index}",
                model=record.get("model", "unknown"), requested_at=timestamp(record.get("timestamp")),
                input_tokens=tokens.get("input", 0) + usage.get("cacheCreationInputTokens", 0),
                output_tokens=tokens.get("output", 0), cached_input_tokens=usage.get("cacheReadInputTokens", 0),
                project="unknown", session_id=str(thread.get("id") or item.path.stem),
            )
            if event:
                events.append(event)
    else:
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            event = make_event(
                kind=KIND, source_key=item.state_key, ordinal=f"message:{index}",
                model=usage.get("model", "unknown"),
                requested_at=timestamp(message.get("timestamp") or thread.get("created")),
                input_tokens=usage.get("inputTokens", 0) + usage.get("cacheCreationInputTokens", 0),
                output_tokens=usage.get("outputTokens", 0), cached_input_tokens=usage.get("cacheReadInputTokens", 0),
                project="unknown", session_id=str(thread.get("id") or item.path.stem),
            )
            if event:
                events.append(event)
    return batch(events, len(ledger) if ledger else len(messages))
