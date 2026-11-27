"""Small, dependency-free helpers shared by agent adapters."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .ir import ParseBatch, UsageEvent, UsageSource
from app.core import sqlite_runtime
from app.core.time import format_utc, parse_external_timestamp

log = logging.getLogger(__name__)

_read_failure_lock = threading.Lock()
_read_failures = 0


def sqlite_read_failure_count() -> int:
    """Number of external SQLite reads that failed and therefore returned no rows.

    External agent databases are read opportunistically: a missing table or a
    write lock must never abort an import.  Returning no rows is still a
    failure that has to be visible, so every failed read also increments this
    counter and logs a warning.  Callers that can attribute a counter delta to
    one source (the importer does) can report it where a user will see it.
    """
    with _read_failure_lock:
        return _read_failures


def reset_sqlite_read_failures() -> int:
    """Reset the read-failure counter and return its previous value."""
    global _read_failures
    with _read_failure_lock:
        previous, _read_failures = _read_failures, 0
        return previous


def _record_sqlite_read_failure(path: Path, sql: str, exc: BaseException) -> None:
    global _read_failures
    with _read_failure_lock:
        _read_failures += 1
    log.warning(
        "agent external sqlite read failed, returning no rows "
        "(path=%s sql=%s): %s: %s",
        path, " ".join(str(sql).split())[:200], type(exc).__name__, exc,
    )


def event_id_for(kind: str, source_identity: str, record_identity: object) -> str:
    """Build an event id from kind + stable source identity + stable record identity.

    ``record_identity`` is supplied by the adapter (database row id, source
    line/record key, or a protocol-specific logical id).  The importer and
    source skeletons must not normalize or recalculate it.
    """

    return f"{kind}:{source_identity}:{record_identity}"


def config_value(software: dict, *names: str) -> str | None:
    config = software.get("config") or {}
    for name in names:
        value = config.get(name)
        if value:
            return str(value)
    return None


def configured_extra_roots(software: dict, kind: str) -> list[Path]:
    """Return configured additional data roots for an adapter.

    Agent software config is intentionally extensible. Accept both the
    current snake_case keys and the reference tool's ``extraRoots`` shape,
    while keeping path parsing in one place for adapters that support more
    than one installation.
    """
    config = software.get("config") or {}
    values = []

    def add(raw) -> None:
        if isinstance(raw, (list, tuple)):
            for item in raw:
                add(item)
            return
        if isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(os.pathsep) if part.strip())

    for key in ("extra_roots", "extraRoots", "extra_data_roots"):
        raw = config.get(key) if isinstance(config, dict) else None
        if isinstance(raw, dict):
            raw = raw.get(kind)
        add(raw)
    normalized_kind = kind.replace("-", "_")
    for key in (
        f"{normalized_kind}_extra_roots",
        f"{normalized_kind}_extra_root",
        f"extra_{normalized_kind}_roots",
        f"extra_{normalized_kind}_root",
    ):
        add(config.get(key) if isinstance(config, dict) else None)

    env_kind = normalized_kind.upper()
    for name in (f"VIBE_USAGE_{env_kind}_EXTRA_ROOTS", f"{env_kind}_EXTRA_ROOTS"):
        add(os.environ.get(name))
    return [Path(value).expanduser() for value in dict.fromkeys(values)]


def configured_root(software: dict, default: Path) -> Path:
    value = config_value(software, "data_root", "path")
    return Path(value).expanduser() if value else default


def safe_int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if result == result and result not in (float("inf"), float("-inf")) else 0.0


def timestamp(value: Any, *, default_timezone: timezone = timezone.utc) -> str | None:
    """Normalize ISO, seconds, milliseconds, or microseconds to UTC Z time."""
    try:
        parsed = parse_external_timestamp(value)
        return format_utc(parsed) if parsed is not None else None
    except ValueError:
        return None


def project_name(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip().replace("\\", "/").rstrip("/")
    if text.startswith("file://"):
        text = text.split("file://", 1)[1].rstrip("/")
    return text.rsplit("/", 1)[-1] or fallback


def read_json(path: Path, default: Any = None) -> Any:
    if path is None:
        return default
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return default


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict]]:
    """Yield valid JSON objects with their one-based physical line number."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line_no, raw in enumerate(source, 1):
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    yield line_no, value
    except OSError:
        return


def walk_files(root: Path, suffixes: Iterable[str], *, max_depth: int | None = None) -> Iterator[Path]:
    suffixes = tuple(suffixes)
    if not root.is_dir():
        return
    try:
        iterator = root.rglob("*")
        for path in iterator:
            if not path.is_file() or (suffixes and not path.name.endswith(suffixes)):
                continue
            if max_depth is not None:
                try:
                    if len(path.relative_to(root).parts) > max_depth:
                        continue
                except ValueError:
                    continue
            yield path
    except OSError:
        return


def source(path: Path, *, key: str | None = None, **context: Any) -> UsageSource:
    return UsageSource(path=path, key=key, context=context)


IDENTITY_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")


def stat_identity(stat) -> dict[str, int]:
    """Content identity of one file: device, inode, size and timestamps.

    The importer cursor and the parse cache share this definition so that
    "this source is unchanged" means exactly one thing.  ``ino`` is what makes
    log rotation visible: a rotated file can keep the old size and, on a
    coarse filesystem, the old mtime.
    """
    return {
        "dev": int(getattr(stat, "st_dev", 0) or 0),
        "ino": int(getattr(stat, "st_ino", 0) or 0),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
    }


def sqlite_rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Read rows from an external agent database without ever raising.

    A missing database file is a normal "this agent is not installed" answer.
    A lock or a malformed file is not: it is counted and logged so that the
    caller can distinguish "no rows" from "could not read", and the caller's
    cursor must not advance past it.
    """
    if not path.is_file():
        return []
    conn = None
    try:
        conn = sqlite_runtime.read_only(path)
        return conn.execute(sql, params).fetchall()
    except (OSError, sqlite3.Error) as exc:
        _record_sqlite_read_failure(path, sql, exc)
        return []
    finally:
        if conn is not None:
            conn.close()


def sqlite_rows_snapshot(
    path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    raise_on_error: bool = False,
) -> list[sqlite3.Row]:
    """Read an external SQLite WAL database through a disposable copy.

    Some agent applications keep a write transaction open for the lifetime of
    the process.  Opening the original file read-only can then return a lock
    error even though its database is otherwise queryable.  Copying the main
    file together with its WAL/SHM companions gives the reader its own stable
    snapshot and never changes the source database.
    """
    path = Path(path)
    if not path.is_file():
        return []
    temp_dir = None
    conn = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="token-board-agent-snapshot-"))
        copied = temp_dir / path.name
        shutil.copy2(path, copied)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(copied) + suffix))
        # Open the disposable copy in writable mode so SQLite can initialize
        # WAL shared-memory metadata when necessary.  query_only keeps the
        # snapshot connection from mutating even this temporary copy.
        conn = sqlite_runtime.connect(
            copied,
            sqlite_runtime.SQLiteProfile(
                "agent_external_snapshot",
                timeout=2.0,
                busy_timeout_ms=2000,
                journal_mode=None,
                foreign_keys=False,
                read_only=False,
                isolation_level=None,
            ),
        )
        conn.execute("PRAGMA query_only=ON")
        return conn.execute(sql, params).fetchall()
    except (OSError, sqlite3.Error) as exc:
        # A snapshot read is the fallback for a database that another process
        # keeps locked.  Swallowing its failure would hide both the lock and
        # the failed fallback, so the failure is always counted and logged;
        # only whether it propagates is the caller's choice.
        _record_sqlite_read_failure(path, sql, exc)
        if raise_on_error:
            raise
        return []
    finally:
        if conn is not None:
            conn.close()
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def sqlite_scalar(path: Path, sql: str, params: tuple[Any, ...] = ()) -> Any:
    rows = sqlite_rows(path, sql, params)
    return rows[0][0] if rows else None


def sqlite_table_exists(path: Path, name: str) -> bool:
    value = sqlite_scalar(path,
                          "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                          (name,))
    return value == 1


def csv_rows(text: str) -> Iterator[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        yield {str(key).strip(): value or "" for key, value in row.items() if key is not None}


def make_event(
    *,
    kind: str,
    source_key: str,
    ordinal: Any,
    model: Any,
    requested_at: str | None,
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    cached_input_tokens: Any = 0,
    reasoning_output_tokens: Any = 0,
    project: str | None = None,
    session_id: str | None = None,
    duration_ms: Any = 0,
    input_includes_cache: bool = False,
    total_tokens: Any | None = None,
) -> UsageEvent | None:
    if requested_at is None:
        return None
    input_count = safe_int(input_tokens)
    output_count = safe_int(output_tokens)
    cache_count = safe_int(cached_input_tokens)
    reasoning_count = safe_int(reasoning_output_tokens)
    if input_count + output_count + cache_count + reasoning_count <= 0:
        return None
    return UsageEvent.from_buckets(
        model=model,
        input_tokens=input_count,
        output_tokens=output_count,
        cached_input_tokens=cache_count,
        reasoning_output_tokens=reasoning_count,
        requested_at=requested_at,
        event_id=event_id_for(kind, source_key, ordinal),
        project=project,
        session_id=session_id,
        duration_ms=duration_ms,
        input_includes_cache=input_includes_cache,
        total_tokens=total_tokens,
    )


def batch(
    events: list[UsageEvent],
    record_count: int,
    *,
    skipped: bool = False,
    warnings: tuple[str, ...] | list[str] = (),
) -> ParseBatch:
    return ParseBatch.from_events(
        events, record_count, skipped=skipped, warnings=warnings,
    )


MIN_PLAUSIBLE_LATENCY_MS = 100
MAX_PLAUSIBLE_LATENCY_MS = 60 * 60 * 1000


def estimated_latency_ms(start_ms: int, end_ms: int) -> int | None:
    """按会话日志时间戳估算的一次请求耗时,区间外返回 None(与 ref/cc-switch 同口径)。"""
    latency = int(end_ms) - int(start_ms)
    return latency if MIN_PLAUSIBLE_LATENCY_MS <= latency <= MAX_PLAUSIBLE_LATENCY_MS else None


def timestamp_ms(value: Any) -> int | None:
    """外部时间戳 → epoch 毫秒;不可解析返回 None。"""
    try:
        parsed = parse_external_timestamp(value)
    except ValueError:
        return None
    return None if parsed is None else int(parsed.timestamp() * 1000)
