"""Small interface shared by all schema transition plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app.db.migrations import SchemaVersion


@dataclass(frozen=True)
class TransitionContext:
    """The immutable inputs and writable shadows for one transition route.

    ``sources`` are read-only source/staged databases.  ``shadows`` are the
    only database files a plugin may mutate.  A companion database may be
    present in ``sources`` for an artifact scope even when it is not published.
    """

    scope: str
    schema_root: Path
    source_timezone: ZoneInfo
    versions: Mapping[str, SchemaVersion]
    sources: Mapping[str, Path]
    shadows: Mapping[str, Path]
    prepare_versions: Mapping[str, SchemaVersion]
    target_versions: Mapping[str, SchemaVersion]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def source(self, database_name: str) -> Path:
        return self.sources[database_name]

    def shadow(self, database_name: str) -> Path:
        return self.shadows[database_name]

    def version(self, database_name: str) -> SchemaVersion:
        return self.versions[database_name]

    def target(self, database_name: str) -> SchemaVersion:
        return self.target_versions[database_name]
