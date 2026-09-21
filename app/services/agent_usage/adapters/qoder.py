"""Qoder and Qoder CN usage adapter (Alibaba's agentic coding platform)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from ..common import (
    batch,
    config_value,
    iter_jsonl,
    make_event,
    project_name,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "qoder"
LABEL = "Qoder"
DESCRIPTION = "Qoder 本地用量"
DEFAULT_PATH = Path.home() / ".qoder" / "projects"

ROUTING_TIERS = {"auto", "ultimate", "performance", "efficient", "lite"}
DEFAULT_MODEL = "qoder-agent"

QODER_CONFIG = {
    "qoder": {
        "kind": "qoder",
        "label": "Qoder",
        "cli_dir_name": ".qoder",
        "cli_env": "QODER_CONFIG_DIR",
        "ide_dir_name": "Qoder",
        "ide_home_env": "QODER_HOME",
        "projects_env": "QODER_PROJECTS_DIR",
        "db_env": "QODER_DB_PATH",
        "legacy_projects_env": "VIBE_USAGE_QODER_PROJECTS",
        "legacy_db_env": "VIBE_USAGE_QODER_DB",
        "default_cli_path": Path.home() / ".qoder" / "projects",
    },
    "qoder-cn": {
        "kind": "qoder-cn",
        "label": "Qoder CN",
        "cli_dir_name": ".qoder-cn",
        "cli_env": "QODERCN_CONFIG_DIR",
        "ide_dir_name": "QoderCN",
        "ide_home_env": "QODER_CN_HOME",
        "projects_env": "QODERCN_PROJECTS_DIR",
        "db_env": "QODERCN_DB_PATH",
        "legacy_projects_env": "VIBE_USAGE_QODER_CN_PROJECTS",
        "legacy_db_env": "VIBE_USAGE_QODER_CN_DB",
        "default_cli_path": Path.home() / ".qoder-cn" / "projects",
    },
}

IDE_DB_RELATIVE = Path("SharedClientCache") / "cache" / "db" / "local.db"


def _env_path(cfg: dict, primary: str, legacy: str) -> str:
    """Read the canonical path variable, then the pre-rename alias."""
    return (os.environ.get(cfg[primary], "").strip()
            or os.environ.get(cfg[legacy], "").strip())


def normalize_qoder_model(key: Any) -> str:
    k = str(key or "").strip()
    if not k:
        return DEFAULT_MODEL
    return f"qoder-{k.lower()}" if k.lower() in ROUTING_TIERS else k


def get_qoder_projects_dir(edition: str = "qoder", software: dict | None = None) -> Path:
    cfg = QODER_CONFIG[edition]
    if software:
        override = config_value(software, "data_root", "path")
        if override:
            root = Path(override).expanduser()
            if root.name == "projects" or not (root / "projects").is_dir():
                return root
            return root / "projects"
    configured = _env_path(cfg, "projects_env", "legacy_projects_env")
    if configured:
        return Path(configured).expanduser()
    cli_env = os.environ.get(cfg["cli_env"], "").strip()
    if cli_env:
        return Path(cli_env).expanduser() / "projects"
    return Path.home() / cfg["cli_dir_name"] / "projects"


def get_qoder_db_path(edition: str = "qoder") -> Path:
    cfg = QODER_CONFIG[edition]
    configured = _env_path(cfg, "db_env", "legacy_db_env")
    if configured:
        return Path(configured).expanduser()
    ide_home = os.environ.get(cfg["ide_home_env"], "").strip()
    if ide_home:
        return Path(ide_home).expanduser() / "cache" / "db" / "local.db"

    ide_name = cfg["ide_dir_name"]
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / ide_name
    elif sys.platform == "win32":
        app_data = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        root = Path(app_data) / ide_name
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        root = Path(xdg) / ide_name
    return root / IDE_DB_RELATIVE


def discover_qoder(software: dict, edition: str = "qoder", stop_event=None) -> list[UsageSource]:
    sources = []
    projects_dir = get_qoder_projects_dir(edition, software)
    if projects_dir.is_dir():
        for path in walk_files(projects_dir, (".jsonl",)):
            sources.append(source(path, context={"edition": edition, "source_type": "transcript"}))

    db_path = get_qoder_db_path(edition)
    if db_path.is_file():
        sources.append(source(db_path, context={"edition": edition, "source_type": "ide_db"}))
    return sources


def _parse_ide_db(item: UsageSource, edition: str, stop_event=None) -> ParseBatch:
    kind = QODER_CONFIG[edition]["kind"]
    ide_columns = (
        "cm.id AS id, cm.session_id AS sessionId, cm.request_id AS requestId, "
        "cm.role AS role, cm.token_info AS tokenInfo, cm.model_info AS modelInfo, "
        "cm.gmt_create AS created"
    )
    query_with_session = (
        f"SELECT {ide_columns}, cs.project_uri AS projectUri, cs.project_name AS projectName, "
        f"cs.preferred_model_info AS preferredModelInfo "
        f"FROM chat_message cm "
        f"LEFT JOIN chat_session cs ON cs.session_id = cm.session_id "
        f"WHERE cm.role IN ('user', 'assistant')"
    )
    query_plain = (
        f"SELECT {ide_columns} FROM chat_message cm WHERE cm.role IN ('user', 'assistant')"
    )

    try:
        rows = sqlite_rows_snapshot(item.path, query_with_session, raise_on_error=True)
    except Exception as err:
        err_msg = str(err).lower()
        if "no such table: chat_session" in err_msg:
            try:
                rows = sqlite_rows_snapshot(item.path, query_plain, raise_on_error=True)
            except Exception as inner_err:
                return batch([], 0, skipped=True, warnings=(f"{kind}: cannot read {item.path}: {inner_err}",))
        elif "no such table: chat_message" in err_msg:
            return batch([], 0)
        else:
            return batch([], 0, skipped=True, warnings=(f"{kind}: cannot read {item.path}: {err}",))

    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            break
        if row["role"] != "assistant":
            continue

        created = row["created"]
        ts = timestamp(created)
        if not ts:
            continue

        token_info_raw = row["tokenInfo"]
        token_info = None
        if token_info_raw:
            try:
                token_info = json.loads(token_info_raw) if isinstance(token_info_raw, str) else token_info_raw
            except (ValueError, TypeError):
                token_info = None
        if not isinstance(token_info, dict):
            continue

        prompt_tokens = safe_int(token_info.get("prompt_tokens"))
        cached_tokens = safe_int(token_info.get("cached_tokens"))
        completion_tokens = safe_int(token_info.get("completion_tokens"))
        if prompt_tokens + completion_tokens == 0:
            continue

        cached = min(prompt_tokens, cached_tokens)
        input_tokens = prompt_tokens - cached

        # Model resolution
        model_info_raw = row["modelInfo"]
        model_info = None
        if model_info_raw:
            try:
                model_info = json.loads(model_info_raw) if isinstance(model_info_raw, str) else model_info_raw
            except (ValueError, TypeError):
                model_info = None

        pref_info_raw = row["preferredModelInfo"] if "preferredModelInfo" in row.keys() else None
        pref_info = None
        if pref_info_raw:
            try:
                pref_info = json.loads(pref_info_raw) if isinstance(pref_info_raw, str) else pref_info_raw
            except (ValueError, TypeError):
                pref_info = None

        model_key = None
        if isinstance(model_info, dict):
            model_key = model_info.get("model_key") or model_info.get("modelKey")
        if not model_key and isinstance(pref_info, dict):
            model_key = pref_info.get("model_key") or pref_info.get("modelKey")
        model = normalize_qoder_model(model_key)

        # Project resolution
        project_uri = str(row["projectUri"]).strip() if "projectUri" in row.keys() and row["projectUri"] else ""
        project = "unknown"
        if project_uri:
            project = project_name(project_uri)
        else:
            p_name = str(row["projectName"]).strip() if "projectName" in row.keys() and row["projectName"] else ""
            if p_name and p_name != ".":
                project = p_name

        session_id = str(row["sessionId"] or "unknown")
        row_id = str(row["id"] or f"{session_id}:{created}")

        event = make_event(
            kind=kind,
            source_key=item.state_key,
            ordinal=f"ide:{row_id}",
            model=model,
            requested_at=ts,
            input_tokens=input_tokens,
            output_tokens=completion_tokens,
            cached_input_tokens=cached,
            project=project,
            session_id=session_id,
        )
        if event:
            events.append(event)

    return batch(events, len(rows))


def _parse_transcripts(item: UsageSource, edition: str, stop_event=None) -> ParseBatch:
    kind = QODER_CONFIG[edition]["kind"]
    fallback_session = item.path.stem
    # One assistant message spans several lines; keep the last usage-bearing record per message id
    usage_by_message: dict[str, dict] = {}
    count = 0
    project_fallback = project_name(item.path.parent.name)

    try:
        for line_no, record in iter_jsonl(item.path):
            count = line_no
            if stop_event is not None and stop_event.is_set():
                break
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            if record_type != "assistant":
                continue

            ts = timestamp(record.get("timestamp"))
            if not ts:
                continue

            session_id = str(record.get("sessionId") or record.get("session_id") or fallback_session)
            cwd = record.get("cwd")
            project = project_name(cwd, project_fallback) if cwd else project_fallback
            message = record.get("message")
            if not isinstance(message, dict):
                continue

            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            msg_id = message.get("id") or record.get("uuid") or f"{item.path.name}:{line_no}"
            key = f"{session_id}|{msg_id}"
            usage_by_message[key] = {
                "usage": usage,
                "model": normalize_qoder_model(message.get("model")),
                "project": project,
                "timestamp": ts,
                "session_id": session_id,
                "message_id": msg_id,
                "line_no": line_no,
            }
    except OSError as err:
        return batch([], 0, skipped=True, warnings=(f"{kind}: cannot read {item.path}: {err}",))

    events = []
    for info in usage_by_message.values():
        usage = info["usage"]
        input_tokens = safe_int(usage.get("input_tokens")) + safe_int(usage.get("cache_creation_input_tokens"))
        cached_tokens = safe_int(usage.get("cache_read_input_tokens") or usage.get("cached_tokens"))
        output_tokens = safe_int(usage.get("output_tokens"))
        if input_tokens + cached_tokens + output_tokens == 0:
            # Credit-billed call: tokens not reported (0), skip
            continue

        event = make_event(
            kind=kind,
            source_key=item.state_key,
            ordinal=f"cli:{info['message_id']}",
            model=info["model"],
            requested_at=info["timestamp"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            project=info["project"],
            session_id=info["session_id"],
        )
        if event:
            events.append(event)

    return batch(events, count)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return discover_qoder(software, edition="qoder", stop_event=stop_event)


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    edition = item.context.get("edition") or "qoder"
    source_type = item.context.get("source_type")
    if source_type == "ide_db" or item.path.suffix.lower() == ".db":
        return _parse_ide_db(item, edition, stop_event)
    return _parse_transcripts(item, edition, stop_event)
