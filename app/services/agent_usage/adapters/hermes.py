"""Hermes multi-profile SQLite adapter."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    configured_root,
    make_event,
    safe_int,
    source,
    sqlite_rows_snapshot,
    timestamp,
)
from ..ir import ParseBatch, UsageSource

KIND = "hermes"
LABEL = "Hermes"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".hermes"
log = logging.getLogger(__name__)

# sessions 表的必需列;token 列按会话累计,会话 id 是稳定记录身份。
REQUIRED_COLUMNS = {"id", "model", "started_at", "input_tokens", "output_tokens",
                    "cache_read_tokens", "reasoning_tokens"}


def _primary_root(software: dict) -> Path:
    env_root = os.environ.get("HERMES_HOME")
    if config_value(software, "data_root", "path") or env_root:
        return configured_root(
            software, Path(env_root).expanduser() if env_root else DEFAULT_PATH)
    if sys.platform == "win32":
        # Hermes Desktop/CLI uses LOCALAPPDATA on Windows. Keep the legacy
        # ~/.hermes store only when the native home does not exist yet.
        local_app_data = Path(os.environ.get(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        native = local_app_data / "hermes"
        legacy = Path.home() / ".hermes"
        return legacy if not native.is_dir() and legacy.is_dir() else native
    return DEFAULT_PATH


def _path_key(path: Path) -> str:
    """数据库的物理身份:符号链接和相对写法指向同一文件时只算一个库。"""
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path)


def _stat_or_none(path: Path, record) -> os.stat_result | None:
    """statIfPresent 语义:缺失返回 None,其它读取错误交给 record。"""
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        record(exc)
        return None


def _is_readable_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.R_OK)
    except OSError:
        return False


def _home_databases(home: Path) -> tuple[list[tuple[Path, str]], OSError | None]:
    """列出一个 Hermes home 下的库:state.db(默认 profile)与 profiles/<name>/state.db。"""
    databases: list[tuple[Path, str]] = []
    error: OSError | None = None

    def record(exc: OSError) -> None:
        nonlocal error
        if error is None:
            error = exc

    default = home / "state.db"
    if _stat_or_none(default, record) is not None:
        databases.append((default, "default"))

    profiles = home / "profiles"
    try:
        children = sorted(profiles.iterdir(), key=lambda child: child.name)
    except FileNotFoundError:
        return databases, error
    except OSError as exc:
        record(exc)
        return databases, error
    for child in children:
        if not child.is_dir():
            continue
        path = child / "state.db"
        info = _stat_or_none(path, record)
        if info is not None and stat.S_ISREG(info.st_mode):
            databases.append((path, child.name))
    return databases, error


def _inspect_home(home: Path) -> tuple[list[tuple[Path, str]], OSError | None, bool]:
    """ref inspectHermesHome:返回 (库列表, 读取错误, 是否至少有一个可读库)。"""
    databases, error = _home_databases(home)
    usable = error is None and any(
        _is_readable_file(path) for path, _ in databases)
    return databases, error, usable


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = _primary_root(software)
    stores: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def add(path: Path, profile: str) -> None:
        key = _path_key(path)
        if key in seen:
            return
        seen.add(key)
        stores.append({"path": path, "profile": profile})

    if root.is_file():
        # 数据根直接指向数据库文件的既有用法
        add(root, "default")
    elif root.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        databases, error = _home_databases(root)
        if error is not None:
            log.debug("Hermes discovery root is unavailable: %s", error)
            warnings.append(f"hermes: 无法读取数据目录 {root}: {error}")
        for path, profile in databases:
            add(path, profile)

    if warnings:
        # 默认 home 读不全时,导入器必须保护全部 Hermes 游标,而不是推进可读
        # 子集并静默丢弃读不到的那个 profile。
        warning_path = root if root.is_dir() else (stores[0]["path"] if stores else root)
        return [source(warning_path, key="hermes-discovery",
                       discovery_warnings=tuple(warnings))]

    for extra in configured_extra_roots(software, KIND):
        databases, error, usable = _inspect_home(extra)
        # 无效或不可读的额外 home 是 skipped,不是“没有数据”的空成功;即使它
        # 指向默认 home/HERMES_HOME,也由下面的 realpath 身份去重。
        if error is not None or not usable:
            reason = "读取失败" if error is not None else "不可用"
            return [source(extra, key="hermes-discovery", discovery_warnings=(
                f"hermes: 额外根目录{reason}，已保留上次同步状态: {extra}",))]
        for path, profile in databases:
            add(path, profile)

    if not stores:
        return []
    # 所有库由同一个 UsageSource 读取:某个会话的哪份拷贝计入需要跨库比较,
    # 逐库一源时各源看不到彼此。
    return [source(stores[0]["path"], stores=stores)]


def _token_total(row) -> int:
    """会话行的累计 token 总量,含 cache write(旧 SQL 存活过滤的判据)。"""
    return (safe_int(row["input_tokens"]) + safe_int(row["output_tokens"])
            + safe_int(row["cache_read_tokens"]) + safe_int(row["cache_write_tokens"])
            + safe_int(row["reasoning_tokens"]))


def _session_identity(row) -> str | None:
    value = row["id"]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _store_rows(path: Path) -> tuple[list | None, str]:
    """读取一个库的会话行;读不出来返回 (None, warning),绝不用空列表冒充。"""
    try:
        columns = {str(row[1]) for row in sqlite_rows_snapshot(
            path, "PRAGMA table_info(sessions)", raise_on_error=True)}
    except (OSError, sqlite3.Error):
        return None, f"hermes: 无法读取 sessions 数据库 {path}"
    if not REQUIRED_COLUMNS.issubset(columns):
        return None, f"hermes: sessions 数据库结构不兼容 {path}"
    cache_write = "cache_write_tokens" if "cache_write_tokens" in columns else "0"
    try:
        rows = sqlite_rows_snapshot(path, f"""SELECT rowid AS session_rowid,id,model,started_at,
            input_tokens,output_tokens,cache_read_tokens,{cache_write} AS cache_write_tokens,
            reasoning_tokens
            FROM sessions ORDER BY rowid""", raise_on_error=True)
    except (OSError, sqlite3.Error):
        return None, f"hermes: 无法读取 sessions 数据库 {path}"
    return rows, ""


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    stores = item.context.get("stores")
    if not isinstance(stores, list) or not stores:
        stores = [{"path": item.path,
                   "profile": item.context.get("profile") or "default"}]

    loaded = []
    warnings = []
    record_count = 0
    for store in stores:
        path = Path(store.get("path") or item.path)
        if not path.is_file():
            continue
        rows, reason = _store_rows(path)
        if rows is None:
            warnings.append(reason)
            continue
        record_count += len(rows)
        loaded.append({"path": path, "profile": str(store.get("profile") or "default"),
                       "rows": rows})
    if warnings:
        # 任一库读失败都是不完整快照:返回空事件并让导入器保留该来源上次的游标。
        return batch([], record_count, skipped=True, warnings=tuple(warnings))

    # 会话 id 就是记录身份,而 token 列是会话累计值:同一会话经由两个库到达
    # (拷贝/迁移的 home、备份目录、符号链接指向的库)只能计一次,保留最完整的
    # 那一行,并列时更早的库(默认 home 先于额外根)胜出。路径身份只防同一物理
    # 文件读两遍,看不见两个文件持有同一记录的情形。
    owners: dict[str, object] = {}
    for store in loaded:
        for row in store["rows"]:
            session_id = _session_identity(row)
            # 没有 id 的行无法证明是另一行的副本,留在本库范围内,不与其兄弟合并。
            if session_id is None:
                continue
            previous = owners.get(session_id)
            if previous is None or _token_total(row) > _token_total(previous):
                owners[session_id] = row

    events = []
    for store in loaded:
        for row in store["rows"]:
            if stop_event is not None and stop_event.is_set():
                return batch(events, record_count, skipped=True)
            session_id = _session_identity(row)
            if session_id is not None and owners.get(session_id) is not row:
                continue  # 更完整的拷贝拥有这个会话
            # 零 token 行的过滤从 SQL 移到这里:跨库比较必须先看到全部行。
            if _token_total(row) == 0:
                continue
            if session_id is not None:
                # event id 只由记录身份决定,不随哪个库胜出而变,重复导入保持幂等。
                source_key, ordinal = f"session:{session_id}", "session"
            else:
                source_key, ordinal = f"store:{store['path']}", row["session_rowid"]
            output = safe_int(row["output_tokens"])
            reasoning = min(output, safe_int(row["reasoning_tokens"]))
            event = make_event(
                kind=KIND, source_key=source_key, ordinal=ordinal,
                model=row["model"] or "unknown", requested_at=timestamp(row["started_at"]),
                input_tokens=safe_int(row["input_tokens"]) + safe_int(row["cache_write_tokens"]),
                output_tokens=output - reasoning,
                cached_input_tokens=row["cache_read_tokens"],
                reasoning_output_tokens=reasoning,
                project=store["profile"], session_id=session_id or "unknown",
            )
            if event:
                events.append(event)
    return batch(events, record_count)
