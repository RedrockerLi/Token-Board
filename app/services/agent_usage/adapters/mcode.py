"""MiniMax Code runtime-state SQLite usage adapter."""

from __future__ import annotations

import hashlib
import os
import re
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
)
from ..ir import ParseBatch, UsageSource

KIND = "mcode"
LABEL = "MiniMax Code"
DESCRIPTION = "MiniMax Code 本地运行时用量"
DEFAULT_PATH_DISPLAY = "~/.minimax/v2/sqlite/runtime-state.sqlite"
DEFAULT_PATH = (
    Path.home() / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite"
)
ALWAYS_SCAN = True

# 运行时库总是位于数据根之下;重定位变量的语义是替换数据根。
DB_RELATIVE_PATH = Path("v2") / "sqlite" / "runtime-state.sqlite"
# mcode CLI 自身的数据根重定位变量,按 CLI 的优先级排列;MCODE_HOME 是
# 宿主既有的覆盖变量,排在两个公开重定位变量之前。
ROOT_ENV_NAMES = ("MCODE_HOME", "MINIMAX_DATA_DIR", "MAVIS_DATA_DIR")
# 剖面数据根:~/.minimax-<profile> 与 ~/.mavis-<profile>。
PROFILE_DIR_PATTERN = re.compile(r"\.(?:minimax|mavis)-.+")

TOKEN_COLUMNS = (
    "session_id",
    "model",
    "ts",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
SESSION_COLUMNS = ("session_id", "workspace_dir", "project_workspace_dir")


def _readable_file(path: Path) -> bool:
    """探测用存在性检查:不可访问的路径按不存在处理。"""
    try:
        return path.is_file()
    except OSError:
        return False


def _explicit_db_path(software: dict) -> Path | None:
    """显式指定的运行时库,此时不再探测其它数据根。"""
    configured = config_value(software, "data_root", "path")
    if configured:
        root = Path(configured).expanduser()
        if root.suffix.lower() in {".sqlite", ".sqlite3", ".db"} or _readable_file(root):
            return root
        # 配置界面给的是数据目录,补上运行时库的相对路径。
        return root / DB_RELATIVE_PATH
    override = os.environ.get("VIBE_USAGE_MCODE_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return None


def _root_from_env() -> Path | None:
    """按 CLI 的优先级解析数据根重定位变量。"""
    for name in ROOT_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        root = Path(value).expanduser()
        if not root.is_absolute():
            raise ValueError(f"{name} 必须是绝对路径: {value!r}")
        return root
    return None


def _profile_dirs(home: Path) -> list[Path]:
    """按目录名排序的 ~/.minimax-<profile> 与 ~/.mavis-<profile> 数据根。"""
    try:
        entries = sorted(home.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return []
    dirs = []
    for entry in entries:
        try:
            if entry.is_dir() and PROFILE_DIR_PATTERN.fullmatch(entry.name):
                dirs.append(entry)
        except OSError:
            continue
    return dirs


def _db_paths(software: dict) -> list[Path]:
    """mcode 的全部运行时库,主库在前。

    探测顺序:显式覆盖(config 数据目录 / VIBE_USAGE_MCODE_DB)、数据根重定位
    变量(MCODE_HOME、MINIMAX_DATA_DIR、MAVIS_DATA_DIR),然后是默认布局
    ~/.minimax、~/.minimax-code、~/.mavis 以及 ~/.minimax-<profile> /
    ~/.mavis-<profile> 剖面根。显式覆盖与重定位变量只产生单个未探测的路径,
    文件缺失时是空结果而不是跳过;默认布局只返回存在的库,并按物理文件去重,
    兼容符号链接不会让同一份账本读两次。
    """
    explicit = _explicit_db_path(software)
    if explicit is not None:
        return [explicit]
    root = _root_from_env()
    if root is not None:
        return [root / DB_RELATIVE_PATH]
    home = Path.home()
    candidates = [
        home / ".minimax",
        home / ".minimax-code",
        home / ".mavis",
    ]
    candidates.extend(_profile_dirs(home))
    paths = []
    seen = set()
    for directory in candidates:
        path = directory / DB_RELATIVE_PATH
        if not _readable_file(path):
            continue
        try:
            identity = os.path.realpath(path)
        except OSError:
            identity = str(path)
        if identity in seen:
            continue
        seen.add(identity)
        paths.append(path)
    return paths


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    paths = [path for path in _db_paths(software) if _readable_file(path)]
    if not paths:
        return []
    # 一个 UsageSource 携带全部库:跨库折叠需要同时看到主库与其它库的行,
    # 游标状态键是主库路径。
    return [source(paths[0], stores=tuple(str(path) for path in paths))]


def _columns(path: Path, table: str) -> set[str]:
    rows = sqlite_rows_snapshot(
        path, f"PRAGMA table_info({table})", raise_on_error=True,
    )
    return {str(row[1]) for row in rows}


def _number(value) -> float:
    return max(0.0, safe_float(value))


def _valid_schema(path: Path) -> bool:
    return (
        set(TOKEN_COLUMNS).issubset(_columns(path, "local_runtime_token_usage"))
        and set(SESSION_COLUMNS).issubset(_columns(path, "local_runtime_sessions"))
    )


def _ledger_identity(row) -> tuple:
    """账本行自身字段的内容身份(不含物理 rowid)。

    身份覆盖整行内容而不是记录位置,同一行被 mcode 迁移/复制到别的数据库后
    rowid 即使不同也仍折叠为一次;联表读出的会话字段只决定项目名,不参与
    身份——否则会话行只存在于其中一个库时,同一笔用量会被计成两行。
    """
    return tuple(row[name] for name in TOKEN_COLUMNS)


def _ledger_digest(identity: tuple) -> str:
    """内容身份的短摘要,作为与存储位置无关的 event 身份。"""
    payload = repr(identity).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()[:16]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    stores = tuple(Path(value) for value in (item.context.get("stores") or ()))
    if not stores:
        stores = (item.path,)
    primary = stores[0]
    if not _readable_file(primary):
        return batch([], 0)

    events = []
    warnings: list[str] = []
    record_count = 0
    # 内容完全相同的行在各库各有一份时按库顺序只算一次;同一库内的重复行是
    # 账本本身的内容,靠出现序号保留,两种情形共用一个计数表。
    occurrences: dict[str, int] = {}
    collapsed_digests: set[str] = set()

    for path in stores:
        if not _readable_file(path):
            # 探测之后消失或不可访问的库直接略过,与参考实现一致。
            continue
        try:
            if not _valid_schema(path):
                if path == primary:
                    return batch([], 0, skipped=True,
                                 warnings=("mcode: runtime-state 数据库结构不兼容",))
                warnings.append(f"mcode: 跳过结构不兼容的数据库 {path}")
                continue
            # Keep the allow-list explicit. Runtime tables also contain raw JSON
            # payloads; selecting them would unnecessarily read message content.
            rows = sqlite_rows_snapshot(path, """
                SELECT
                    token.session_id, token.model, token.ts,
                    token.input_tokens, token.output_tokens,
                    token.reasoning_tokens, token.cache_read_tokens,
                    token.cache_write_tokens,
                    session.workspace_dir, session.project_workspace_dir,
                    token.rowid AS token_rowid
                FROM local_runtime_token_usage AS token
                LEFT JOIN local_runtime_sessions AS session
                  ON session.session_id = token.session_id
                ORDER BY token.rowid
            """, raise_on_error=True)
        except (OSError, sqlite3.Error, ValueError) as exc:
            # 首个数据根是正在运行的 CLI 会用的库,它的失败保留上一次同步
            # 状态;后续旧库/剖面库出错只记警告,不遮蔽主库的数据。
            if path == primary:
                return batch([], 0, skipped=True,
                             warnings=("mcode: 无法读取 runtime-state 数据库",))
            warnings.append(f"mcode: 无法读取 {path},已跳过: {exc}")
            continue

        previous_rowid = None
        for row in rows:
            if stop_event is not None and stop_event.is_set():
                return batch(events, record_count + len(rows), skipped=True,
                             warnings=warnings)
            # 会话表里同一 session_id 有多行时,LEFT JOIN 会把同一账本行扇出
            # 多份;结果按 rowid 排序,扇出的副本紧挨在一起,只取第一份。
            token_rowid = row[10]
            if token_rowid == previous_rowid:
                continue
            previous_rowid = token_rowid
            digest = _ledger_digest(_ledger_identity(row))
            if digest in collapsed_digests:
                # 同一行在更早的库里出现过:这是迁移/复制留下的副本。
                continue
            session_id = str(row[0] or "").strip()
            requested_at = timestamp(row[2])
            if not session_id or requested_at is None:
                continue
            input_tokens = _number(row[3]) + _number(row[7])
            output_tokens = _number(row[4])
            reasoning_tokens = _number(row[5])
            cache_read = _number(row[6])
            if input_tokens + output_tokens + reasoning_tokens + cache_read <= 0:
                continue
            workspace = row[9] or row[8]
            model = str(row[1] or "unknown").strip() or "unknown"
            occurrence = occurrences.get(digest, 0) + 1
            occurrences[digest] = occurrence
            event = make_event(
                kind=KIND,
                # 来源身份与项目名、物理库无关,迁移复制后同一行的 event_id
                # 保持不变;没有公开事件 id 的账本行用内容摘要做记录身份,
                # 同一行重复出现时靠序号区分。
                source_key=f"session:{session_id}",
                ordinal=f"{digest}:{occurrence}",
                model=model,
                requested_at=requested_at,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cache_read,
                reasoning_output_tokens=reasoning_tokens,
                project=project_name(workspace),
                session_id=session_id,
            )
            if event:
                events.append(event)
        record_count += len(rows)
        if len(stores) > 1:
            collapsed_digests.update(occurrences)

    # 后续库读取失败时仍返回主库的行(导入器照常写入),整批标记 skipped
    # 以保留同步状态并把警告写入运行错误。
    return batch(events, record_count, skipped=bool(warnings),
                 warnings=warnings)
