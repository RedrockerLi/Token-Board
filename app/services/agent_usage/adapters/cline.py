"""Cline task history adapter."""

import json
import os
import sys
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, read_json, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "cline"
LABEL = "Cline"
DEFAULT_PATH = Path.home() / ".cline"
HOSTS = ("Code", "Cursor", "Windsurf", "VSCodium", "Code - Insiders", "Trae", "Trae CN")


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    override = os.environ.get("VIBE_USAGE_CLINE_DIRS", "").strip()
    if override:
        return [Path(value).expanduser() for value in override.split(os.pathsep) if value]
    home = Path.home()
    roots = [home / ".cline"]
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif __import__("sys").platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    roots.extend(base / host / "User" / "globalStorage" / "saoudrizwan.claude-dev" for host in HOSTS)
    return roots


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    for root in _roots(software):
        history = read_json(root / "state" / "taskHistory.json")
        if not isinstance(history, list):
            continue
        for item in history:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            path = root / "tasks" / str(item["id"]) / "ui_messages.json"
            if path.is_file():
                identity = str(item.get("ulid") or item["id"])
                try:
                    stat = path.stat()
                    score = (stat.st_size, stat.st_mtime_ns)
                except OSError:
                    continue
                current = candidates.get(identity)
                if current is None or score > current[0]:
                    candidates[identity] = (score, path, item)
    return [source(
        path,
        key=f"task:{identity}",
        task_id=str(item["id"]),
        task_identity=identity,
        task=item,
    ) for identity, (_, path, item) in candidates.items()]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    messages = read_json(item.path)
    if not isinstance(messages, list):
        return batch([], 0)
    task = item.context.get("task") or {}
    project = project_name(task.get("cwdOnTaskInitialization") or task.get("shadowGitConfigWorkTree") or task.get("cwd"))
    fallback_model = str(task.get("modelId") or "cline-unknown")
    events = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        ts = timestamp(message.get("ts"))
        if message.get("type") == "say" and message.get("say") == "api_req_started":
            try:
                info = json.loads(message.get("text") or "{}")
            except (ValueError, TypeError):
                continue
            if not isinstance(info, dict):
                continue
            event = make_event(
                kind=KIND, source_key=f"task:{item.context.get('task_identity') or item.context.get('task_id')}", ordinal=index,
                model=info.get("model") or fallback_model, requested_at=ts,
                input_tokens=info.get("tokensIn", 0) + info.get("cacheWrites", 0),
                output_tokens=info.get("tokensOut", 0), cached_input_tokens=info.get("cacheReads", 0),
                project=project, session_id=item.context.get("task_id"),
            )
            if event:
                events.append(event)
    return batch(events, len(messages))
