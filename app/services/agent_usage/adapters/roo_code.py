"""Roo Code extension task adapter."""

import json
import logging
import os
import sys
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, read_json, source, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "roo-code"
LABEL = "Roo Code"
DEFAULT_PATH = Path.home() / ".config" / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline"
HOSTS = ("Code", "Cursor", "Windsurf", "VSCodium", "Code - Insiders", "Trae", "Trae CN")
log = logging.getLogger(__name__)


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [base / host / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" for host in HOSTS]


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    out = []
    for root in _roots(software):
        tasks = root / "tasks"
        index = read_json(tasks / "_index.json")
        items = index.get("entries") if isinstance(index, dict) else None
        if not isinstance(items, list):
            items = []
            try:
                for child in tasks.iterdir():
                    if child.is_dir() and not child.name.startswith(("_", ".")):
                        value = read_json(child / "history_item.json")
                        if isinstance(value, dict):
                            items.append(value)
            except OSError:
                log.debug("Roo Code task directory is unavailable", exc_info=True)
        for task in items:
            if isinstance(task, dict) and task.get("id"):
                path = tasks / str(task["id"]) / "ui_messages.json"
                if path.is_file():
                    out.append(source(path, task_id=str(task["id"]), task=task))
    return out


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    messages = read_json(item.path)
    if not isinstance(messages, list):
        return batch([], 0)
    task = item.context.get("task") or {}
    project = project_name(task.get("workspace"))
    fallback = str(task.get("apiConfigName") or "roo-unknown")
    events = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "say" or msg.get("say") != "api_req_started":
            continue
        try:
            info = json.loads(msg.get("text") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(info, dict):
            continue
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=index, model=info.get("model") or fallback,
            requested_at=timestamp(msg.get("ts")),
            input_tokens=info.get("tokensIn", 0) + info.get("cacheWrites", 0), output_tokens=info.get("tokensOut", 0),
            cached_input_tokens=info.get("cacheReads", 0), project=project, session_id=item.context.get("task_id"),
        )
        if event:
            events.append(event)
    return batch(events, len(messages))
