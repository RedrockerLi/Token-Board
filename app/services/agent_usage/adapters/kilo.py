"""Kilo CLI/Desktop usage adapter.

Kilo CLI and the embedded server in Kilo Desktop share one XDG data directory
holding ``kilo.db``: a session/message/part schema whose ``data`` JSON column
carries the model, project path and token counters.  One statement reads the
allow-listed fields of a consistent snapshot; malformed JSON or an
incompatible schema must fail the store instead of silently dropping rows,
because a silent drop would let the importer prune its sync state.

Forks regenerate message/part ids but keep the original request ids and
timestamps, so inherited calls are removed from usage while new work in the
same session stays real.
"""

from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

from ..common import (
    batch,
    config_value,
    make_event,
    project_name,
    safe_float,
    source,
    sqlite_rows_snapshot,
    timestamp,
    timestamp_ms,
)
from ..ir import ParseBatch, UsageSource

KIND = "kilo"
LABEL = "Kilo"
DESCRIPTION = "Kilo CLI/Desktop 共享 SQLite 用量"
KILO_DB_NAME = "kilo.db"
DEFAULT_PATH = Path.home() / ".local" / "share" / "kilo" / KILO_DB_NAME
# Kilo keeps its store open while it runs and writes through a WAL, so the
# main database file's stat signature cannot be trusted to detect new usage.
ALWAYS_SCAN = True

# One statement gives a consistent SQLite view, including a live WAL.  Only
# individual allow-listed fields cross the SQL boundary.
USAGE_SQL = """
 SELECT 'session' AS kind, s.id AS recordId, s.id AS sessionId,
   s.parent_id AS parentId, s.directory AS directory, s.time_created AS columnCreated,
   NULL AS dataCreated, NULL AS messageId, NULL AS role, NULL AS model,
   NULL AS cwdPath, NULL AS rootPath,
   NULL AS inputTokens, NULL AS outputTokens, NULL AS reasoningTokens,
   NULL AS cacheReadTokens, NULL AS cacheWriteTokens,
   NULL AS stepStart, NULL AS generationId, NULL AS vercelId
 FROM session s
 UNION ALL
 SELECT 'message', m.id, m.session_id, NULL, NULL, m.time_created,
   json_extract(m.data, '$.time.created'), m.id,
   json_extract(m.data, '$.role'),
   coalesce(json_extract(m.data, '$.modelID'), json_extract(m.data, '$.modelId'),
     json_extract(m.data, '$.model.modelID'), json_extract(m.data, '$.model.id')),
   json_extract(m.data, '$.path.cwd'), json_extract(m.data, '$.path.root'),
   json_extract(m.data, '$.tokens.input'), json_extract(m.data, '$.tokens.output'),
   json_extract(m.data, '$.tokens.reasoning'), json_extract(m.data, '$.tokens.cache.read'),
   json_extract(m.data, '$.tokens.cache.write'), NULL, NULL, NULL
 FROM message m
 UNION ALL
 SELECT 'part', p.id, p.session_id, NULL, NULL, p.time_created, NULL, p.message_id, NULL,
   coalesce(json_extract(p.data, '$.model.modelID'), json_extract(p.data, '$.model.modelId'),
     json_extract(p.data, '$.model.id')),
   NULL, NULL,
   json_extract(p.data, '$.tokens.input'), json_extract(p.data, '$.tokens.output'),
   json_extract(p.data, '$.tokens.reasoning'), json_extract(p.data, '$.tokens.cache.read'),
   json_extract(p.data, '$.tokens.cache.write'), json_extract(p.data, '$.time.start'),
   json_extract(p.data, '$.generationID'), json_extract(p.data, '$.vercelID')
 FROM part p WHERE json_extract(p.data, '$.type') = 'step-finish'
 ORDER BY columnCreated, recordId
"""

# Generic routing words need a namespace so they cannot borrow another tool's
# price entry; concrete provider/model ids (including slash-bearing ones) are
# preserved as-is.
ROUTING_LABELS = frozenset({
    "auto", "default", "default-model", "balanced", "free", "fast",
    "performance", "efficient", "lite", "ultimate",
})


def resolve_kilo_model(raw) -> str:
    """Keep concrete model ids; namespace only bare routing labels."""
    model = raw.strip() if isinstance(raw, str) else ""
    if not model:
        return "unknown"
    return f"kilo-{model.lower()}" if model.lower() in ROUTING_LABELS else model


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _data_root() -> Path:
    """Kilo's XDG data directory on every platform (like xdg-basedir)."""
    xdg = os.environ.get("XDG_DATA_HOME", "").replace("\r", "").replace("\n", "")
    if xdg:
        expanded = Path(xdg).expanduser()
        if expanded.is_absolute():
            return expanded / "kilo"
    return Path.home() / ".local" / "share" / "kilo"


def _db_path() -> Path | None:
    """The single configured default database, or None for ``:memory:``."""
    configured = os.environ.get("KILO_DB", "").strip()
    if configured == ":memory:":
        return None
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_absolute():
            return expanded
        return _data_root() / expanded
    return _data_root() / KILO_DB_NAME


def _normalize_root(value: str) -> Path:
    expanded = Path(value).expanduser()
    return expanded if expanded.is_absolute() else expanded.resolve()


def _candidates(software: dict) -> tuple[list[Path], bool, bool, bool]:
    """Store candidates, whether they are explicit, whether dirs are OK, and
    whether the store is unreadable by another process (``:memory:``)."""
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()], True, True, False
    override = os.environ.get("VIBE_USAGE_KILO_DIRS", "").strip()
    if override:
        values = [part.strip() for part in override.split(os.pathsep) if part.strip()]
        return [_normalize_root(value) for value in values], True, True, False
    path = _db_path()
    explicit = bool(os.environ.get("KILO_DB", "").strip())
    return ([] if path is None else [path]), explicit, False, path is None


def find_kilo_dbs(software: dict) -> tuple[list[Path], list[str]]:
    """Every readable Kilo database, canonicalized, plus discovery warnings.

    A missing default means Kilo has not written data yet; a missing explicit
    target, a wrong path type, an unusable permission or a dangling symlink is
    a broken store and reported, because silently returning nothing for it
    would make the importer prune that store's sync state.
    """
    candidates, explicit, directory_roots, unsupported = _candidates(software)
    warnings: list[str] = []
    result: list[Path] = []
    seen = set()
    if unsupported:
        warnings.append("kilo: KILO_DB=:memory: 无法从另一进程读取，已跳过")
        return result, warnings
    for candidate in candidates:
        try:
            is_directory = candidate.is_dir()
            if is_directory and not directory_roots:
                raise ValueError("数据库路径是目录而不是文件")
            path = candidate / KILO_DB_NAME if is_directory else candidate
            if path.is_dir():
                raise ValueError("数据库路径不是文件")
            if not path.is_file():
                raise FileNotFoundError(
                    errno.ENOENT, "数据库文件不存在", str(path))
            # A read-only open checks actual permission without reading data.
            with open(path, "rb"):
                pass
            canonical = path.resolve()
        except (OSError, ValueError) as exc:
            if (not explicit and isinstance(exc, OSError)
                    and exc.errno == errno.ENOENT
                    and not os.path.lexists(candidate)):
                # A genuinely absent default is "no data yet"; a dangling
                # symlink still exists as an entry and is a broken store.
                continue
            warnings.append(f"kilo: 无法读取数据库 {candidate}: {exc}")
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
    return result, warnings


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    stores, warnings = find_kilo_dbs(software)
    if not stores:
        if not warnings:
            return []
        configured = config_value(software, "data_root", "path")
        missing = (Path(configured).expanduser() if configured
                   else _db_path() or _data_root())
        return [source(missing, key="kilo-discovery",
                       discovery_warnings=tuple(warnings))]
    return [source(stores[0], key="kilo-store", stores=tuple(stores),
                   discovery_warnings=tuple(warnings))]


def _tokens_for(row: dict) -> dict:
    return {
        "input": safe_float(row.get("inputTokens")),
        "output": safe_float(row.get("outputTokens")),
        "cached": safe_float(row.get("cacheReadTokens")),
        "reasoning": safe_float(row.get("reasoningTokens")),
        "cache_write": safe_float(row.get("cacheWriteTokens")),
    }


def _footprint(tokens: dict) -> float:
    return sum(tokens.values())


def _row_footprint(row: dict) -> float:
    return _footprint(_tokens_for(row))


def _first_timestamp(*values) -> tuple[int | None, str | None]:
    """First usable timestamp among the candidates, with its epoch millis."""
    for value in values:
        if value is None or value == "":
            continue
        try:
            normalized = timestamp(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if normalized is None:
            continue
        millis = timestamp_ms(value)
        if millis is None:
            continue
        return millis, normalized
    return None, None


def _collect(rows, sessions: dict, messages: dict, parts: dict) -> None:
    """Merge one store's rows into the session/message/part maps.

    The same record id can appear in a copied or richer store, so the copy
    with the larger token footprint wins, and a row that carries a model wins
    over an equal-footprint copy that does not.
    """
    for source_row in rows:
        row = dict(source_row)
        session_id = str(row["sessionId"])
        if row["kind"] == "session":
            candidate = {
                "parent_id": _text(row["parentId"]),
                "directory": _text(row["directory"]),
                "created_at": timestamp_ms(row["columnCreated"]),
            }
            prior = sessions.get(session_id)
            if (prior is None
                    or (candidate["created_at"] is not None
                        and (prior["created_at"] is None
                             or candidate["created_at"] < prior["created_at"]))):
                sessions[session_id] = candidate
            continue
        key = (session_id, row["recordId"])
        collection = messages if row["kind"] == "message" else parts
        previous = collection.get(key)
        if (previous is None
                or _row_footprint(row) > _row_footprint(previous)
                or (_row_footprint(row) == _row_footprint(previous)
                    and _text(row["model"]) and not _text(previous["model"]))):
            collection[key] = {
                **row, "sessionId": session_id,
                "timestamp": _first_timestamp(row["dataCreated"], row["columnCreated"]),
            }


def _project_for(message: dict, session: dict | None) -> str:
    # Kilo may record path.root='/' even though cwd identifies the workspace.
    value = (_text(message.get("cwdPath"))
             or (session or {}).get("directory")
             or _text(message.get("rootPath")))
    return project_name(value)


def _request_ids(call: dict) -> list[str]:
    return [prefix + value for prefix, value in
            (("g:", call.get("generation_id")), ("v:", call.get("vercel_id")))
            if value]


def _same_call(a: dict, b: dict) -> bool:
    if a["ts"] != b["ts"]:
        return False
    a_ids, b_ids = _request_ids(a), _request_ids(b)
    if a_ids and b_ids:
        return any(value in b_ids for value in a_ids)
    return a["model"] == b["model"] and a["tokens"] == b["tokens"]


def _session_created(sessions: dict, session_id: str) -> int | float:
    """A session's creation instant; an unknown session sorts last."""
    created = (sessions.get(session_id) or {}).get("created_at")
    return float("inf") if created is None else created


def _dedupe_replay(calls: list[dict], sessions: dict) -> list[dict]:
    """Drop calls a fork inherited from an earlier session.

    A cross-session match may suppress usage only when it precedes the target
    session's creation and a matching earlier physical copy exists, so repeated
    local calls stay real.  Occurrences are paired per target session.
    """
    ordered = sorted(calls, key=lambda call: (
        _session_created(sessions, call["session_id"]),
        call["ts"], call["message_id"], call["record_id"],
    ))
    by_time: dict[int, list[dict]] = {}
    survivors: list[dict] = []
    paired: dict[str, set[int]] = {}
    for call in ordered:
        created = (sessions.get(call["session_id"]) or {}).get("created_at")
        time = call["ts"]
        candidates = by_time.setdefault(time, [])
        used = paired.setdefault(call["session_id"], set())
        original = None
        if created is not None and time < created:
            for other in candidates:
                if (other["session_id"] != call["session_id"]
                        and _session_created(
                            sessions, other["session_id"]) < created
                        and other["index"] not in used
                        and _same_call(call, other)):
                    original = other
                    break
        if original is not None:
            used.add(original["index"])
            if (_footprint(call["tokens"]) > _footprint(original["tokens"])
                    or (_footprint(call["tokens"]) == _footprint(original["tokens"])
                        and call["has_step_model"] and not original["has_step_model"])):
                original["tokens"] = call["tokens"]
                original["model"] = call["model"]
                original["has_step_model"] = call["has_step_model"]
            continue
        candidates.append(call)
        survivors.append(call)
    return survivors


def _calls(messages: dict, parts: dict, sessions: dict) -> list[dict]:
    parts_by_message: dict[tuple[str, str], list[dict]] = {}
    for part in parts.values():
        parts_by_message.setdefault(
            (part["sessionId"], part["messageId"]), []).append(part)
    calls: list[dict] = []
    for message in messages.values():
        if message["role"] != "assistant":
            continue
        session = sessions.get(message["sessionId"])

        def add(row: dict, timed, *, step: bool) -> None:
            tokens = _tokens_for(row)
            if timed is None or _footprint(tokens) == 0:
                return
            calls.append({
                "index": len(calls),
                "session_id": message["sessionId"],
                "message_id": str(message["messageId"]),
                "record_id": str(row["recordId"]),
                "ts": timed[0], "requested_at": timed[1], "tokens": tokens,
                "model": resolve_kilo_model(
                    _text(row.get("model")) or message.get("model")),
                "has_step_model": bool(step and _text(row.get("model"))),
                "project": _project_for(message, session),
                "generation_id": _text(row.get("generationId")),
                "vercel_id": _text(row.get("vercelId")),
                "from_step": step,
            })

        steps = [part for part in parts_by_message.get(
            (message["sessionId"], message["messageId"]), [])
            if _row_footprint(part) > 0]
        if steps:
            # step-finish gained its own `time` only later; older steps fall
            # back to the message's time before the part row's column, which
            # the JSON→SQLite migration and forks stamp with their own instant.
            for step in steps:
                add(step, _first_timestamp(
                    step["stepStart"], message["dataCreated"],
                    step["columnCreated"], message["columnCreated"]), step=True)
        else:
            add(message, message["timestamp"], step=False)
    return calls


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    warnings = list(item.context.get("discovery_warnings") or ())
    stores = item.context.get("stores")
    paths = ([Path(store) for store in stores if store]
             if isinstance(stores, (list, tuple)) and stores else [item.path])
    sessions: dict[str, dict] = {}
    messages: dict[tuple[str, str], dict] = {}
    parts: dict[tuple[str, str], dict] = {}
    record_count = 0
    for path in paths:
        if stop_event is not None and stop_event.is_set():
            return batch([], record_count, skipped=True, warnings=warnings)
        try:
            rows = sqlite_rows_snapshot(path, USAGE_SQL, raise_on_error=True)
        except (OSError, sqlite3.Error) as exc:
            warnings.append(f"kilo: 无法读取 {path}: {exc}")
            continue
        record_count += len(rows)
        _collect(rows, sessions, messages, parts)
    if warnings:
        return batch([], record_count, skipped=True, warnings=warnings)
    events = []
    for call in _dedupe_replay(_calls(messages, parts, sessions), sessions):
        if stop_event is not None and stop_event.is_set():
            return batch(events, record_count, skipped=True)
        tokens = call["tokens"]
        event = make_event(
            kind=KIND,
            # 记录身份与物理库路径无关:同一份库被复制/搬家后 event_id 不变,
            # 收据机制因此不会重复入账。
            source_key="store",
            ordinal=f"p:{call['record_id']}" if call["from_step"] else f"m:{call['record_id']}",
            model=call["model"],
            requested_at=call["requested_at"],
            # IR 没有写缓存列,按仓库惯例折进 input;cache.read 由 IR 叠加到
            # prompt,落库口径为 prompt = input + cache.write + cache.read。
            input_tokens=tokens["input"] + tokens["cache_write"],
            output_tokens=tokens["output"],
            cached_input_tokens=tokens["cached"],
            reasoning_output_tokens=tokens["reasoning"],
            project=call["project"],
            session_id=call["session_id"],
        )
        if event:
            events.append(event)
    return batch(events, record_count)
