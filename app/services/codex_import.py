"""Generic agent usage import with a Codex session adapter."""

from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .codex_replay import replay_skip_counts

CODEX_HOME = Path.home() / ".codex"
CODEX_DIR = CODEX_HOME / "sessions"


def _iso_z_to_sqlite(ts: str) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", name)
    return match.group(1) if match else path.stem


def _open_maybe_gz(path: Path):
    return (gzip.open(path, "rt", encoding="utf-8")
            if path.name.endswith(".gz") else
            open(path, "r", encoding="utf-8"))


def _iter_session_files(roots=None, stop_event: threading.Event | None = None):
    roots = list(roots or (CODEX_DIR,))
    seen = set()
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("*.jsonl", "*.jsonl.gz"):
            for path in root.rglob(pattern):
                if stop_event is not None and stop_event.is_set():
                    return
                if not path.is_file():
                    continue
                key = path.resolve()
                if key not in seen:
                    seen.add(key)
                    yield path


def _parse_session(path: Path, stop_event: threading.Event | None = None,
                   skip_token_count: int = 0):
    """Return ``(line_count, events)`` for one Codex transcript."""
    rows = []
    session_id = None
    project = None
    model = "codex"
    previous_cumulative = None
    line_no = 0
    raw_token_seen = 0
    skip_token_count = max(0, int(skip_token_count or 0))
    with _open_maybe_gz(path) as source:
        for raw in source:
            if stop_event is not None and stop_event.is_set():
                return None
            line_no += 1
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            event_type = obj.get("type")
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if event_type == "session_meta":
                session_id = (payload.get("id") or payload.get("session_id")
                              or session_id)
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                project = (payload.get("project") or payload.get("cwd")
                           or payload.get("workdir") or git.get("repository_url")
                           or project)
            elif event_type == "turn_context":
                value = payload.get("model")
                if isinstance(value, str) and value.strip():
                    model = value.strip()
                project = (payload.get("project") or payload.get("cwd")
                           or payload.get("workdir") or project)
            elif event_type == "event_msg" and payload.get("type") == "token_count":
                raw_token_seen += 1
                info = payload.get("info") or {}
                last = info.get("last_token_usage") if isinstance(info, dict) else None
                if isinstance(info, dict):
                    value = info.get("model") or payload.get("model")
                    if isinstance(value, str) and value.strip():
                        model = value.strip()
                cumulative = (info.get("total_token_usage")
                               if isinstance(info, dict) else None)
                cumulative_total = (cumulative.get("total_tokens")
                                    if isinstance(cumulative, dict) else None)
                previous_total = (previous_cumulative.get("total_tokens")
                                  if isinstance(previous_cumulative, dict) else None)
                duplicate = (isinstance(cumulative_total, (int, float))
                             and cumulative_total > 0
                             and cumulative_total == previous_total)
                if duplicate:
                    continue
                if not isinstance(last, dict) and isinstance(cumulative, dict):
                    if isinstance(previous_cumulative, dict):
                        last = {
                            key: _token_int(cumulative.get(key))
                            - _token_int(previous_cumulative.get(key))
                            for key in ("input_tokens", "output_tokens",
                                        "cached_input_tokens", "reasoning_output_tokens")
                        }
                    else:
                        last = cumulative
                if not isinstance(last, dict):
                    continue
                if isinstance(cumulative, dict):
                    previous_cumulative = dict(cumulative)
                if raw_token_seen <= skip_token_count:
                    continue
                timestamp = _iso_z_to_sqlite(obj.get("timestamp"))
                if timestamp is None:
                    continue
                current_session = session_id or _session_id_from_path(path)
                prompt_tokens = _token_int(last.get("input_tokens"))
                completion_tokens = _token_int(last.get("output_tokens"))
                cache_read_tokens = _token_int(last.get("cached_input_tokens"))
                total_tokens = _token_int(last.get("total_tokens"))
                if total_tokens <= 0:
                    # Some Codex versions omit last_token_usage.total_tokens.
                    total_tokens = prompt_tokens + completion_tokens
                rows.append({
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "total_tokens": total_tokens,
                    "requested_at": timestamp,
                    "event_id": f"codex:{current_session}:{line_no}",
                    "project": project if isinstance(project, str) else None,
                    "session_id": current_session,
                })
    return line_no, rows


def _software_roots(software: dict):
    config = software.get("config") or {}
    configured = config.get("data_root") or config.get("path")
    if configured:
        root = Path(str(configured)).expanduser()
        if root.name in {"sessions", "archived_sessions"}:
            return [root]
        return [root / "sessions", root / "archived_sessions"]
    configured_home = Path(os.environ.get("CODEX_HOME", str(CODEX_HOME))).expanduser()
    default_live = CODEX_DIR
    if CODEX_DIR == CODEX_HOME / "sessions":
        default_live = configured_home / "sessions"
    roots = [default_live]
    archived = configured_home / "archived_sessions"
    if archived != default_live:
        roots.append(archived)
    return roots


def _opencode_db_path(software: dict) -> Path:
    config = software.get("config") or {}
    configured = config.get("data_root") or config.get("path")
    if configured:
        value = Path(str(configured)).expanduser()
        # Treat a configured path as a data directory unless it explicitly
        # names a database.  This keeps a not-yet-created OpenCode directory
        # usable on first setup as well as an existing direct .db path.
        return value if value.is_file() or value.suffix.lower() == ".db" \
            else value / "opencode.db"
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _opencode_timestamp(value) -> str | None:
    if isinstance(value, (int, float)):
        # OpenCode stores milliseconds since epoch in some releases and an ISO
        # string in others.
        seconds = float(value) / (1000 if value > 10_000_000_000 else 1)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    return _iso_z_to_sqlite(str(value)) if value else None


def _parse_opencode(path: Path, stop_event: threading.Event | None = None,
                    skip_token_count: int = 0):
    """Read OpenCode's local ``message`` table into the shared UsageEvent IR."""
    rows = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                               timeout=2)
        conn.row_factory = sqlite3.Row
        query = (
            "SELECT rowid AS message_rowid,session_id,"
            "json_extract(data,'$.time.created') AS created,"
            "json_extract(data,'$.modelID') AS model_id,"
            "json_extract(data,'$.tokens') AS tokens,"
            "json_extract(data,'$.path.root') AS root_path "
            "FROM message ORDER BY rowid"
        )
        source_rows = conn.execute(query).fetchall()
        conn.close()
    except (OSError, sqlite3.Error):
        return 0, []
    for source in source_rows:
        if stop_event is not None and stop_event.is_set():
            return None
        timestamp = _opencode_timestamp(source["created"])
        if timestamp is None or not source["model_id"]:
            continue
        tokens = source["tokens"]
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except (TypeError, ValueError):
                continue
        if not isinstance(tokens, dict):
            continue
        prompt = _token_int(tokens.get("input"))
        completion = _token_int(tokens.get("output"))
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        cache_read = _token_int(cache.get("read"))
        if prompt <= 0 and completion <= 0:
            continue
        session_id = str(source["session_id"] or "unknown")
        root = source["root_path"]
        project = Path(str(root)).name if root else "unknown"
        rows.append({
            "model": str(source["model_id"]),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "total_tokens": prompt + completion,
            "requested_at": timestamp,
            "event_id": f"opencode:{session_id}:{source['message_rowid']}",
            "project": project,
            "session_id": session_id,
        })
    return len(source_rows), rows


def _software_sources(software: dict,
                      stop_event: threading.Event | None = None):
    if software.get("agent_kind") == "opencode":
        path = _opencode_db_path(software)
        return [path] if path.is_file() else []
    return list(_iter_session_files(_software_roots(software), stop_event))


AGENT_PARSERS = {
    # Every parser returns the same UsageEvent IR consumed by the importer:
    # model, token buckets, timestamp, project and session identity.
    "codex": _parse_session,
    "opencode": _parse_opencode,
}


def _import_software(pdb, software: dict,
                     stop_event: threading.Event | None = None) -> int:
    software_id = int(software["id"])
    agent_kind = software.get("agent_kind")
    # Resolve Codex dynamically so tests/extensions can replace the adapter,
    # while other agent kinds continue to use the registry contract.
    parser = (_parse_session if agent_kind == "codex"
              else AGENT_PARSERS.get(agent_kind))
    if parser is None:
        return 0
    files = list(_software_sources(software, stop_event))
    if not files:
        return 0
    replay_skips = (replay_skip_counts(files, stop_event)
                    if agent_kind == "codex" else {})

    conn = pdb._connect()
    inserted = 0
    try:
        cursor = conn.execute(
            "SELECT cursor_json FROM agent_software_runtime WHERE software_id=?",
            (software_id,),
        ).fetchone()
        try:
            states = json.loads(cursor[0]) if cursor and cursor[0] else {}
        except (TypeError, ValueError):
            states = {}
        if not isinstance(states, dict):
            states = {}

        for path in files:
            if stop_event is not None and stop_event.is_set():
                return inserted
            path_key = str(path)
            try:
                before = path.stat()
            except OSError:
                continue
            previous = states.get(path_key)
            same_mtime = (
                previous.get("mtime_ns") == int(before.st_mtime_ns)
                if isinstance(previous, dict) and "mtime_ns" in previous
                else isinstance(previous, dict)
                and previous.get("mtime") == int(before.st_mtime)
            )
            if (isinstance(previous, dict)
                    and previous.get("size") == before.st_size
                    and same_mtime):
                continue

            skip_token_count = replay_skips.get(path_key, 0)
            parsed = (parser(path, stop_event,
                             skip_token_count=skip_token_count)
                      if skip_token_count else parser(path, stop_event))
            if parsed is None:
                return inserted
            line_count, rows = parsed
            try:
                after = path.stat()
            except OSError:
                continue
            stable = (after.st_size == before.st_size and
                      int(after.st_mtime_ns) == int(before.st_mtime_ns))
            file_inserted = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    if pdb._insert_agent_usage_row(
                            conn, software_id, row["model"], row["prompt_tokens"],
                            row["completion_tokens"], row["cache_read_tokens"],
                            row["total_tokens"], row["requested_at"],
                            row["event_id"], row.get("project"),
                            row.get("session_id")):
                        file_inserted += 1
                if stable:
                    states[path_key] = {
                        "size": after.st_size,
                        "mtime": int(after.st_mtime),
                        "mtime_ns": int(after.st_mtime_ns),
                        "last_line": line_count,
                        "session_id": _session_id_from_path(path),
                        "parsed_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                    }
                    conn.execute(
                        "UPDATE agent_software_runtime SET cursor_json=?,"
                        "last_scan_at=?,last_error=NULL WHERE software_id=?",
                        (json.dumps(states, ensure_ascii=False, separators=(",", ":")),
                         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         software_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            inserted += file_inserted
    except Exception as exc:
        try:
            conn.execute(
                "UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                (str(exc)[:500], software_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()
    return inserted


def import_once(pdb, stop_event: threading.Event | None = None) -> int:
    """Import one idempotent pass for every present registered software."""
    if stop_event is not None and stop_event.is_set():
        return 0
    inserted = 0
    for software in pdb.get_agent_software():
        if software.get("agent_kind") in AGENT_PARSERS:
            inserted += _import_software(pdb, software, stop_event)
    return inserted
