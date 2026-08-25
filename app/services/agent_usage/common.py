"""Small, dependency-free helpers shared by agent adapters."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from .ir import ParseBatch, UsageEvent, UsageSource
from app.core import sqlite_runtime
from app.core.time import format_utc, parse_external_timestamp


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


def sqlite_rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    if not path.is_file():
        return []
    conn = None
    try:
        conn = sqlite_runtime.read_only(path)
        return conn.execute(sql, params).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        if conn is not None:
            conn.close()


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
        input_includes_cache=input_includes_cache,
        total_tokens=total_tokens,
    )


def batch(events: list[UsageEvent], record_count: int) -> ParseBatch:
    return ParseBatch.from_events(events, record_count)
