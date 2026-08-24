"""Generic source/cursor importer for all registered agent adapters."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, Mapping

from .ir import ParseBatch, UsageEvent, UsageSource
from .registry import get_adapter


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_stat(previous: dict, stat) -> bool:
    if not isinstance(previous, dict):
        return False
    if "mtime_ns" in previous:
        return previous.get("mtime_ns") == int(stat.st_mtime_ns)
    return previous.get("mtime") == int(stat.st_mtime)


def _coerce_batch(raw, *, kind: str, source_item: UsageSource) -> ParseBatch | None:
    if raw is None:
        return None
    if isinstance(raw, ParseBatch):
        return raw
    # Compatibility for the pre-IR Codex adapter contract: (line_count, rows)
    # where rows are request_log-shaped dictionaries.
    if isinstance(raw, tuple) and len(raw) == 2:
        record_count, rows = raw
        events = []
        for row in rows or []:
            if isinstance(row, UsageEvent):
                events.append(row)
                continue
            if not isinstance(row, dict):
                continue
            try:
                events.append(UsageEvent(
                    model=row.get("model", "unknown"),
                    prompt_tokens=row.get("prompt_tokens", 0),
                    completion_tokens=row.get("completion_tokens", 0),
                    cache_read_tokens=row.get("cache_read_tokens", 0),
                    total_tokens=row.get("total_tokens", 0),
                    requested_at=row.get("requested_at") or "",
                    event_id=row.get("event_id") or f"{kind}:{source_item.state_key}:{len(events)}",
                    project=row.get("project"), session_id=row.get("session_id"),
                ))
            except (TypeError, ValueError):
                continue
        return ParseBatch.from_events(events, int(record_count or 0))
    raise TypeError(f"agent adapter {kind} returned an invalid ParseBatch")


def _parse(adapter, source_item: UsageSource, stop_event, *, skip_token_count: int,
           parser_overrides: Mapping[str, Callable] | None):
    overrides = parser_overrides or {}
    parser = overrides.get(adapter.KIND)
    if parser is not None:
        # Compatibility parsers predate UsageSource and intentionally receive
        # a Path.  Keep this branch explicit instead of relying on function
        # identity (which is fragile for bound callables and test doubles).
        if skip_token_count:
            return parser(source_item.path, stop_event,
                          skip_token_count=skip_token_count)
        return parser(source_item.path, stop_event)
    return adapter.parse(source_item, stop_event,
                         skip_token_count=skip_token_count)


def _import_software(pdb, software: dict, stop_event=None,
                     parser_overrides: Mapping[str, Callable] | None = None) -> int:
    software_id = int(software["id"])
    kind = str(software.get("agent_kind") or "").strip().lower()
    adapter = get_adapter(kind)
    if adapter is None:
        return 0
    sources = list(adapter.discover(software, stop_event))
    if not sources:
        return 0
    replay_skips = {}
    prepare = getattr(adapter, "replay_skips", None)
    if prepare is not None:
        replay_skips = prepare(sources, stop_event)

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

        for source_item in sources:
            if stop_event is not None and stop_event.is_set():
                return inserted
            try:
                before = source_item.path.stat()
            except OSError:
                continue
            state_key = source_item.state_key
            previous = states.get(state_key)
            always_scan = bool(getattr(adapter, "ALWAYS_SCAN", False))
            if (not always_scan and isinstance(previous, dict)
                    and previous.get("size") == before.st_size
                    and _same_stat(previous, before)):
                continue
            skip = replay_skips.get(str(source_item.path), 0)
            raw = _parse(adapter, source_item, stop_event,
                         skip_token_count=skip, parser_overrides=parser_overrides)
            parsed = _coerce_batch(raw, kind=kind, source_item=source_item)
            if parsed is None:
                return inserted
            try:
                after = source_item.path.stat()
            except OSError:
                continue
            stable = after.st_size == before.st_size and int(after.st_mtime_ns) == int(before.st_mtime_ns)
            file_inserted = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event in parsed.events:
                    if pdb._insert_agent_usage_row(
                            conn, software_id, event.model, event.prompt_tokens,
                            event.completion_tokens, event.cache_read_tokens,
                            event.total_tokens, event.requested_at, event.event_id,
                            event.project, event.session_id):
                        file_inserted += 1
                if stable:
                    states[state_key] = {
                        "size": after.st_size,
                        "mtime": int(after.st_mtime),
                        "mtime_ns": int(after.st_mtime_ns),
                        "record_count": parsed.record_count,
                        "parsed_at": _utc_now(),
                    }
                    conn.execute(
                        "UPDATE agent_software_runtime SET cursor_json=?,last_scan_at=?,last_error=NULL WHERE software_id=?",
                        (json.dumps(states, ensure_ascii=False, separators=(",", ":")), _utc_now(), software_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            inserted += file_inserted
    except Exception as exc:
        try:
            conn.execute("UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                         (str(exc)[:500], software_id))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()
    return inserted


def import_once(pdb, stop_event=None, *, parser_overrides: Mapping[str, Callable] | None = None) -> int:
    if stop_event is not None and stop_event.is_set():
        return 0
    inserted = 0
    for software in pdb.get_agent_software():
        kind = str(software.get("agent_kind") or "").strip().lower()
        if kind in get_registered_kinds():
            inserted += _import_software(pdb, software, stop_event, parser_overrides)
    return inserted


def get_registered_kinds() -> tuple[str, ...]:
    from .registry import ADAPTERS
    return tuple(ADAPTERS)
