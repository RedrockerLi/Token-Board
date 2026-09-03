"""Descriptor-driven loading of version-specific database transitions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import sqlite_runtime
from app.db.migrations import DATABASE_NAMES, MigrationError, SchemaVersion


TRANSITION_SCOPES = frozenset({
    "local-pair",
    "token-board-artifact",
    "dashboard-artifact",
})


@dataclass(frozen=True)
class VersionRange:
    """An inclusive database-version range used by a transition route."""

    major: int
    min_minor: int | None = None
    max_minor: int | None = None

    def matches(self, version: SchemaVersion) -> bool:
        if version.major != self.major:
            return False
        if self.min_minor is not None and version.minor < self.min_minor:
            return False
        if self.max_minor is not None and version.minor > self.max_minor:
            return False
        return True


@dataclass(frozen=True)
class VersionTarget:
    """A route target, either a fixed version or the current schema tip."""

    version: SchemaVersion | None = None
    same: bool = False

    def resolve(self, schema_root: Path, database_name: str,
                current: SchemaVersion | None = None) -> SchemaVersion:
        if self.same:
            if current is None:
                raise MigrationError(
                    f"same route target requires a current version for {database_name}")
            return current
        if self.version is not None:
            return self.version
        directory = schema_root / database_name / "v1"
        versions = []
        for path in directory.glob("*.sql"):
            try:
                stem = path.name.split("_", 1)[0]
                major, minor = (int(value) for value in stem.split("-", 1))
            except (ValueError, IndexError):
                continue
            versions.append(SchemaVersion(major, minor))
        if not versions:
            raise MigrationError(f"no schema files for {database_name} V1")
        return max(versions)


@dataclass(frozen=True)
class TransitionRoute:
    scope: str
    current: dict[str, VersionRange]
    prepare: dict[str, VersionTarget]
    target: dict[str, VersionTarget]

    def matches(self, versions: dict[str, SchemaVersion]) -> bool:
        return all(
            database in versions and selector.matches(versions[database])
            for database, selector in self.current.items()
        )


@dataclass(frozen=True)
class Transition:
    transition_id: str
    directory: Path
    descriptor: Path
    entrypoint: Path
    checksum: str
    order: int
    databases: tuple[str, ...]
    strategy: str
    routes: tuple[TransitionRoute, ...]

    def load(self):
        name = "token_board_transition_" + self.transition_id.replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, self.entrypoint)
        if spec is None or spec.loader is None:
            raise MigrationError(f"cannot load transition module {self.entrypoint}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def matching_routes(self, scope: str,
                        versions: dict[str, SchemaVersion]) -> tuple[TransitionRoute, ...]:
        return tuple(route for route in self.routes
                     if route.scope == scope and route.matches(versions))


def _digest(directory: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _version_range(value, descriptor: Path) -> VersionRange:
    if isinstance(value, str):
        if value.endswith(".*"):
            try:
                return VersionRange(int(value[:-2]))
            except ValueError as exc:
                raise MigrationError(f"invalid version range in {descriptor}: {value}") from exc
        try:
            major, minor = (int(item) for item in value.split(".", 1))
            return VersionRange(major, minor, minor)
        except (ValueError, IndexError) as exc:
            raise MigrationError(f"invalid version range in {descriptor}: {value}") from exc
    if not isinstance(value, dict) or "major" not in value:
        raise MigrationError(f"invalid version range in {descriptor}: {value!r}")
    try:
        major = int(value["major"])
        minimum = value.get("min_minor")
        maximum = value.get("max_minor")
        minimum = None if minimum is None else int(minimum)
        maximum = None if maximum is None else int(maximum)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"invalid version range in {descriptor}: {value!r}") from exc
    if minimum is not None and maximum is not None and minimum > maximum:
        raise MigrationError(f"inverted version range in {descriptor}: {value!r}")
    return VersionRange(major, minimum, maximum)


def _version_target(value, descriptor: Path) -> VersionTarget:
    if value == "latest":
        return VersionTarget()
    if value == "same":
        return VersionTarget(same=True)
    if isinstance(value, str):
        try:
            major, minor = (int(item) for item in value.split(".", 1))
            return VersionTarget(SchemaVersion(major, minor))
        except (ValueError, IndexError) as exc:
            raise MigrationError(f"invalid route target in {descriptor}: {value}") from exc
    if isinstance(value, dict):
        try:
            return VersionTarget(SchemaVersion(int(value["major"]), int(value["minor"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise MigrationError(f"invalid route target in {descriptor}: {value!r}") from exc
    raise MigrationError(f"invalid route target in {descriptor}: {value!r}")


def _routes(data, descriptor: Path) -> tuple[TransitionRoute, ...]:
    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise MigrationError(f"transition {descriptor} must declare non-empty routes")
    routes = []
    for raw in raw_routes:
        if not isinstance(raw, dict):
            raise MigrationError(f"invalid route in {descriptor}: {raw!r}")
        scope = raw.get("scope")
        if scope not in TRANSITION_SCOPES:
            raise MigrationError(f"unknown transition scope in {descriptor}: {scope!r}")
        current = raw.get("current")
        prepare = raw.get("prepare", {})
        target = raw.get("target", {})
        if (not isinstance(current, dict) or not current or
                not isinstance(prepare, dict) or not isinstance(target, dict)):
            raise MigrationError(f"invalid route maps in {descriptor}")
        routes.append(TransitionRoute(
            scope,
            {name: _version_range(value, descriptor) for name, value in current.items()},
            {name: _version_target(value, descriptor) for name, value in prepare.items()},
            {name: _version_target(value, descriptor) for name, value in target.items()},
        ))
    return tuple(routes)


def discover(schema_root: str | Path) -> list[Transition]:
    root = Path(schema_root).resolve() / "transitions"
    if not root.is_dir():
        return []
    result: list[Transition] = []
    seen: set[str] = set()
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        descriptor = directory / "transition.json"
        if not descriptor.is_file():
            continue
        try:
            data = json.loads(descriptor.read_text(encoding="utf-8"))
            transition_id = str(data["id"])
            entrypoint = directory / str(data["entrypoint"])
            order = int(data.get("order", 1000))
            databases = tuple(str(name) for name in data["databases"])
            strategy = str(data["strategy"])
            routes = _routes(data, descriptor)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(f"invalid transition descriptor: {descriptor}") from exc
        if not databases or any(name not in DATABASE_NAMES for name in databases):
            raise MigrationError(
                f"transition {transition_id} names an unknown database: {databases}")
        if len(set(databases)) != len(databases):
            raise MigrationError(f"transition {transition_id} repeats a database")
        if transition_id in seen:
            raise MigrationError(f"duplicate transition id: {transition_id}")
        if not entrypoint.is_file():
            raise MigrationError(f"transition entrypoint not found: {entrypoint}")
        if any(
                (set(route.current) | set(route.prepare) | set(route.target))
                - set(databases) for route in routes):
            raise MigrationError(
                f"transition {transition_id} route references undeclared database")
        if strategy not in {"shadow-barrier", "rebuild-shadow"}:
            raise MigrationError(
                f"transition {transition_id} has unknown strategy: {strategy}")
        seen.add(transition_id)
        # Generated bytecode must not change the transition identity between
        # two runs.  The descriptor and every source/data file are otherwise
        # part of the checksum, so changing a transition after publication is
        # detected rather than silently replayed.
        files = sorted(
            path for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
        result.append(Transition(
            transition_id, directory, descriptor, entrypoint,
            _digest(directory, files), order, databases, strategy, routes))
    result.sort(key=lambda item: (item.order, item.transition_id))
    return result


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def transition_record(path: Path, transition: Transition) -> tuple[str, str] | None:
    if not path.exists():
        return None
    conn = sqlite_runtime.connect(path, "schema_upgrade")
    try:
        if not _table_exists(conn, "schema_transitions"):
            return None
        row = conn.execute(
            "SELECT checksum,generation_id FROM schema_transitions "
            "WHERE transition_id=?", (transition.transition_id,)
        ).fetchone()
        if row and row[0] != transition.checksum:
            raise MigrationError(
                f"checksum mismatch for transition {transition.transition_id}")
        return tuple(row) if row else None
    finally:
        conn.close()


def record_transition(path: Path, transition: Transition,
                      generation_id: str) -> None:
    conn = sqlite_runtime.connect(path, "schema_upgrade")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_transitions(" 
            "transition_id TEXT PRIMARY KEY,checksum TEXT NOT NULL," 
            "generation_id TEXT NOT NULL,applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_transitions(transition_id,checksum,generation_id,applied_at) "
            "VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(transition_id) DO UPDATE SET checksum=excluded.checksum, "
            "generation_id=excluded.generation_id,applied_at=excluded.applied_at",
            (transition.transition_id, transition.checksum, generation_id),
        )
        conn.commit()
    finally:
        conn.close()
