"""OpenCode SQLite/legacy JSON message adapter."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    make_event,
    project_name,
    safe_float,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "opencode"
LABEL = "OpenCode"
DESCRIPTION = "OpenCode 本地数据库用量"
DEFAULT_PATH_DISPLAY = "~/.local/share/opencode/opencode.db"
DEFAULT_PATH = Path.home() / ".local" / "share" / "opencode"
# SQLite/JSON stores are external and can change without the auth file mtime.
ALWAYS_SCAN = True


def _default_roots() -> list[Path]:
    """默认根:XDG 优先,Windows 上追加 LOCALAPPDATA 原生存储。

    只展开当前存在的原生目录;不存在的候选根在发现阶段也不会命中,
    因此 XDG 结果不受影响。
    """
    roots = [DEFAULT_PATH]
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            native = Path(local_app_data).expanduser() / "opencode"
            if native.is_dir():
                roots.append(native)
    return roots


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        roots = [Path(configured).expanduser()]
    else:
        override = os.environ.get("VIBE_USAGE_OPENCODE_DIRS", "").strip()
        roots = ([Path(value).expanduser() for value in override.split(os.pathsep)
                  if value.strip()]
                 if override else _default_roots())
    roots.extend(configured_extra_roots(software, KIND))
    return list(dict.fromkeys(roots))


def _store_for_root(root: Path) -> dict | None:
    root = root.expanduser()
    if root.is_file() and root.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return {"format": "sqlite", "path": root}
    if root.is_file():
        return None
    db = root / "opencode.db"
    if db.is_file():
        return {"format": "sqlite", "path": db}
    messages = root if root.name == "message" else root / "storage" / "message"
    if messages.is_dir():
        return {"format": "json", "path": messages}
    return None


def _canonical(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _json_representative(directory: Path) -> Path | None:
    return next(iter(walk_files(directory, (".json",))), None)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    stores = []
    seen = set()
    warnings = []
    warning_paths = []
    configured_extra = {
        _canonical(root) for root in configured_extra_roots(software, KIND)
    }
    for root in _roots(software):
        store = _store_for_root(root)
        if store is None:
            if _canonical(root) in configured_extra:
                warnings.append(f"opencode: 额外数据根目录不可用: {root}")
                warning_paths.append(root)
            continue
        canonical = _canonical(Path(store["path"]))
        if canonical in seen:
            continue
        seen.add(canonical)
        store = {**store, "path": canonical}
        if store["format"] == "json":
            representative = _json_representative(canonical)
            if representative is None:
                continue
        else:
            representative = canonical
        stores.append((store, representative))

    if not stores:
        if warnings:
            return [source(
                warning_paths[0] if warning_paths else _roots(software)[0],
                key="opencode-discovery",
                discovery_warnings=warnings,
            )]
        return []
    representative = stores[0][1]
    return [source(
        representative,
        format="multi",
        stores=[store for store, _ in stores],
        discovery_warnings=warnings,
    )]


def _row_tokens(row: dict) -> dict | None:
    tokens = row.get("tokens")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except (TypeError, ValueError):
            return None
    return tokens if isinstance(tokens, dict) else None


def _token_size(row: dict) -> float:
    tokens = _row_tokens(row) or {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    return sum(safe_float(tokens.get(key)) for key in ("input", "output", "reasoning")) \
        + safe_float(cache.get("read")) + safe_float(cache.get("write"))


def _read_legacy_messages(path: Path) -> list[dict]:
    """读取 1.x 的 message 表。"""
    schema = sqlite_rows_snapshot(
        path, "PRAGMA table_info(message)", raise_on_error=True,
    )
    has_id = any(str(row[1]) == "id" for row in schema)
    identity = "id" if has_id else "rowid"
    rows = sqlite_rows_snapshot(path, f"""SELECT
        {identity} AS message_id, session_id AS session_id,
        json_extract(data,'$.role') AS role,
        json_extract(data,'$.time.created') AS created,
        COALESCE(json_extract(data,'$.modelID'),
                 json_extract(data,'$.model.modelID')) AS model_id,
        json_extract(data,'$.tokens') AS tokens,
        json_extract(data,'$.path.root') AS path_root
        FROM message ORDER BY rowid""", raise_on_error=True)
    return [dict(row) for row in rows]


def _read_session_messages(path: Path, tables: set[str]) -> list[dict]:
    """读取 2.x 的事件投影 session_message。

    2.x 把同样的计价数据写进 session_message:``type`` 是角色,``data.model.id``
    是模型,``data.tokens`` 是计数器。消息数据里没有 ``path``,项目改取会话表的
    ``directory``;会话表名为 session_v2 或 session,均可作为 LEFT JOIN 目标,
    两者都不存在时只做占位。只选列/JSON 表达式,不读消息正文与工具负载。
    """
    if "session_v2" in tables:
        session_table = "session_v2"
    elif "session" in tables:
        session_table = "session"
    else:
        session_table = None
    join = (f"LEFT JOIN {session_table} s ON s.id = m.session_id"
            if session_table else "")
    directory = "s.directory AS directory" if session_table else "NULL AS directory"
    rows = sqlite_rows_snapshot(path, f"""SELECT
        m.id AS message_id, m.session_id AS session_id,
        m.type AS role,
        COALESCE(json_extract(m.data,'$.time.created'),
                 m.time_created) AS created,
        COALESCE(json_extract(m.data,'$.model.id'),
                 json_extract(m.data,'$.modelID')) AS model_id,
        json_extract(m.data,'$.tokens') AS tokens,
        {directory}
        FROM session_message m
        {join}
        WHERE m.type IN ('user', 'assistant')
        ORDER BY m.rowid""", raise_on_error=True)
    return [dict(row) for row in rows]


def _read_sqlite(path: Path) -> list[dict]:
    """读取一个 SQLite 存储,兼容 1.x 与 2.x 两种表结构。

    同一库内两种表示同时存在时,legacy 行先出:按 (session, message id) 合并
    同一条消息只计一次,等量副本保留 legacy 行,project 名沿用旧表示的取值。
    """
    if not path.is_file():
        return []
    # 标识符在 SQLite 里不区分大小写,表名探测同样按小写匹配,避免大小写
    # 变体被误判成不认识的表结构。
    tables = {str(row[0]) for row in sqlite_rows_snapshot(
        path,
        "SELECT lower(name) FROM sqlite_master WHERE type = 'table' "
        "AND lower(name) IN ('message', 'session_message', 'session', 'session_v2')",
        raise_on_error=True,
    )}
    if "message" not in tables and "session_message" not in tables:
        raise ValueError("不认识的表结构(没有 message / session_message 表)")
    rows: list[dict] = []
    if "message" in tables:
        rows.extend(_read_legacy_messages(path))
    if "session_message" in tables:
        rows.extend(_read_session_messages(path, tables))
    return rows


def _read_json(directory: Path) -> list[dict]:
    rows = []
    for path in walk_files(directory, (".json",)):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        nested_model = data.get("model") if isinstance(data.get("model"), dict) else {}
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
        rows.append({
            "message_id": data.get("id") or path.stem,
            "session_id": path.parent.name,
            "role": data.get("role"),
            "created": time_data.get("created"),
            "model_id": data.get("modelID") or nested_model.get("modelID"),
            "tokens": data.get("tokens"),
            "path_root": path_data.get("root"),
        })
    return rows


def _event(item: UsageSource, row: dict, record_identity: str):
    tokens = _row_tokens(row)
    if not tokens or row.get("role") != "assistant":
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    session_id = str(row.get("session_id") or "unknown")
    return make_event(
        kind=KIND,
        source_key=f"session:{session_id}",
        ordinal=record_identity,
        model=row.get("model_id") or "unknown",
        requested_at=row.get("created") if isinstance(row.get("created"), str)
        else timestamp(row.get("created")),
        # 该存储只写一个未分类的 cache.write 总量,IR 没有单独的写缓存列,
        # 按仓库惯例折进 input(与 mcode/alma/claude-code 相同);tokens.input
        # 是不含 cache.read 的净输入,cache.read 由 IR 叠加到 prompt,落库口径
        # 为 prompt = input + cache.write + cache.read。
        input_tokens=safe_int(tokens.get("input")) + safe_int(cache.get("write")),
        output_tokens=tokens.get("output", 0),
        cached_input_tokens=cache.get("read", 0),
        reasoning_output_tokens=tokens.get("reasoning", 0),
        # 2.x 消息行没有 path,项目回退到 session 行的 directory。
        project=project_name(row.get("path_root") or row.get("directory")),
        session_id=session_id,
    )


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    stores = item.context.get("stores")
    if not isinstance(stores, list):
        stores = [{"format": item.context.get("format"), "path": item.path}]
    warnings = list(item.context.get("discovery_warnings") or [])
    records = {}
    record_count = 0
    for store in stores:
        path = Path(store["path"])
        try:
            rows = (_read_sqlite(path) if store.get("format") == "sqlite"
                    else _read_json(path))
        except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
            warnings.append(f"opencode: 无法读取 {path}: {exc}")
            continue
        record_count += len(rows)
        for index, row in enumerate(rows):
            if stop_event is not None and stop_event.is_set():
                return batch([], record_count, skipped=True, warnings=warnings)
            created = timestamp(row.get("created"))
            if created is None:
                continue
            row = {**row, "created": created}
            session_id = str(row.get("session_id") or "unknown")
            message_id = row.get("message_id")
            # 记录身份同时是合并键与事件序号:同一 session 内消息 id 唯一;没有
            # id 的行用存储路径加行下标兜底,保证不同物理行(两代表示、不同
            # 存储)永远不会落到同一个 event_id 上被静默去重。
            record_identity = str(message_id) if message_id else f"{path}:{index}"
            key = (session_id, record_identity)
            old = records.get(key)
            if old is None or _token_size(row) > _token_size(old):
                records[key] = row

    if warnings:
        return batch([], record_count, skipped=True, warnings=warnings)
    events = []
    for key, row in records.items():
        event = _event(item, row, key[1])
        if event:
            events.append(event)
    return batch(events, record_count)
