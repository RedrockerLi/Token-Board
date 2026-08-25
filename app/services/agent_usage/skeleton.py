"""Small source skeletons for adapter discovery and record extraction.

These classes intentionally stop at source I/O. Protocol-specific extraction
stays in the adapter, while importer cursor/stat/transaction handling remains
centralized in ``importer.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .common import iter_jsonl, read_json, sqlite_rows
from .ir import UsageSource


@dataclass(frozen=True)
class AdapterSpec:
    kind: str
    discover: Callable
    parse: Callable
    always_scan: bool = False
    replay_skips: Callable | None = None

    @classmethod
    def from_module(cls, module) -> "AdapterSpec":
        return cls(
            kind=str(module.KIND),
            discover=module.discover,
            parse=module.parse,
            always_scan=bool(getattr(module, "ALWAYS_SCAN", False)),
            replay_skips=getattr(module, "replay_skips", None),
        )


class JsonlSourceAdapter:
    """Read JSONL records while preserving source line identity."""

    @staticmethod
    def records(source: UsageSource) -> Iterator[tuple[int, dict[str, Any]]]:
        yield from iter_jsonl(source.path)


class JsonSourceAdapter:
    """Read a JSON document without deciding its protocol shape."""

    @staticmethod
    def document(source: UsageSource) -> Any:
        return read_json(source.path)


class SqliteSourceAdapter:
    """Read rows from an external SQLite database through the read-only profile."""

    @staticmethod
    def rows(source: UsageSource, sql: str,
             params: tuple[Any, ...] = ()) -> list:
        return sqlite_rows(source.path, sql, params)


__all__ = [
    "AdapterSpec", "JsonSourceAdapter", "JsonlSourceAdapter",
    "SqliteSourceAdapter",
]
