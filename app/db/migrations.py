"""Private SQL migration primitives used by the Python upgrade engine.

Version-specific data transforms live under ``schema/transitions`` and are
orchestrated by ``app.db.schema_upgrade``.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from app.core import sqlite_runtime


VERSION_RE = re.compile(r"^(\d+)-(\d+)_([a-z0-9][a-z0-9_]*)\.sql$")
TOKEN_BOARD_DATABASE_NAME = "token-board"
DASHBOARD_DATABASE_NAME = "dashboard"
DATABASE_NAMES = frozenset({TOKEN_BOARD_DATABASE_NAME, DASHBOARD_DATABASE_NAME})


class MigrationError(RuntimeError):
    """Raised when a schema cannot be safely selected or upgraded."""


@dataclass(frozen=True, order=True)
class SchemaVersion:
    major: int
    minor: int

    @property
    def user_version(self) -> int:
        return self.major * 10_000 + self.minor

    @classmethod
    def from_user_version(cls, value: int) -> "SchemaVersion":
        if value < 0:
            raise MigrationError(f"negative PRAGMA user_version: {value}")
        return cls(value // 10_000, value % 10_000)


@dataclass(frozen=True)
class MigrationStep:
    version: SchemaVersion
    path: Path
    checksum: str


def schema_dir_for(db_path: str, name: str) -> str:
    """Return the recommended schema root for ``name``.

    ``db_path`` is retained in the API because callers may operate on shadow
    copies; the repository schema is still located relative to the data dir.
    """
    if name not in DATABASE_NAMES:
        raise MigrationError(f"unknown database name: {name}")
    return str(Path(db_path).resolve().parent.parent / "schema")


def apply_sql_migrations(db_path: str, schema_dir: str,
                         database_name: str | None = None,
                         target: SchemaVersion | None = None) -> None:
    """Apply only the SQL portion of a compatible migration path."""
    database_name = database_name or _infer_database_name(db_path, schema_dir)
    fd = os.open(db_path + ".migrate.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _run_locked(db_path, schema_dir, database_name, target)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def migrate(db_path: str, schema_dir: str,
            database_name: str | None = None) -> None:
    """Compatibility alias for fixture builders.

    Production startup and database façades use the schema-upgrade
    coordinator, not this low-level SQL-only primitive.
    """
    apply_sql_migrations(db_path, schema_dir, database_name)


def _infer_database_name(db_path: str, schema_dir: str) -> str:
    parts = {part.lower() for part in Path(schema_dir).parts}
    stem = Path(db_path).stem.lower()
    if DASHBOARD_DATABASE_NAME in parts or DASHBOARD_DATABASE_NAME in stem:
        return DASHBOARD_DATABASE_NAME
    return TOKEN_BOARD_DATABASE_NAME


def _resolve_schema_dir(schema_dir: str, database_name: str,
                        current: SchemaVersion | None) -> Path:
    supplied = Path(schema_dir).resolve()
    if not supplied.is_dir():
        # Compatibility for a path that disappeared while packaging.  A
        # schema/<database> path is otherwise a supported database root.
        if supplied.name in DATABASE_NAMES and supplied.parent.is_dir():
            print(f"warning: --schema-dir {supplied} is deprecated; use "
                  f"{supplied.parent}", file=sys.stderr)
            supplied = supplied.parent
        else:
            raise MigrationError(f"schema dir not found: {schema_dir}")

    # Explicit leaf directories are useful for transition tools and tests.
    if any(VERSION_RE.match(p.name) for p in supplied.glob("*.sql")):
        return supplied

    database_root = supplied
    if supplied.name not in DATABASE_NAMES:
        database_root = supplied / database_name
    majors: list[tuple[int, Path]] = []
    for child in database_root.glob("v*"):
        if child.is_dir() and child.name[1:].isdigit():
            majors.append((int(child.name[1:]), child))
    if not majors:
        raise MigrationError(
            f"no schema/{database_name}/vN directory below {supplied}")
    majors.sort(key=lambda item: item[0])
    if current is None:
        return majors[-1][1]
    latest = majors[-1][0]
    if current.major != latest:
        raise MigrationError(
            f"{database_name} schema is V{current.major}.{current.minor}, but this "
            f"program uses V{latest}; run schema/transitions/"
            f"{current.major}-to-{latest} before starting")
    return majors[-1][1]


def _sql_steps(schema_dir: str | Path) -> list[MigrationStep]:
    """Validate and numerically order ``major-minor_description.sql`` files."""
    directory = Path(schema_dir)
    if not directory.is_dir():
        raise MigrationError(f"schema dir not found: {directory}")
    steps: list[MigrationStep] = []
    for path in directory.glob("*.sql"):
        match = VERSION_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"bad migration filename: {path.name} "
                "(need major-minor_description.sql)")
        version = SchemaVersion(int(match.group(1)), int(match.group(2)))
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        steps.append(MigrationStep(version, path, checksum))
    if not steps:
        raise MigrationError(f"no migration files in {directory}")
    steps.sort(key=lambda step: step.version)
    versions = [step.version for step in steps]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration version in {directory}")
    if len({step.version.major for step in steps}) != 1:
        raise MigrationError(f"mixed major versions in {directory}")
    return steps


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _database_version(conn: sqlite3.Connection,
                      database_name: str) -> SchemaVersion | None:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if table:
        row = conn.execute(
            "SELECT major,minor,database_name FROM schema_version WHERE id=1"
        ).fetchone()
        if row:
            if row[2] != database_name:
                raise MigrationError(
                    f"database identity is {row[2]!r}, expected {database_name!r}")
            pragma = conn.execute("PRAGMA user_version").fetchone()[0]
            canonical = SchemaVersion(int(row[0]), int(row[1]))
            if pragma != canonical.user_version:
                raise MigrationError(
                    "schema_version and PRAGMA user_version disagree: "
                    f"V{canonical.major}.{canonical.minor} vs {pragma}")
            return canonical

    pragma = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if pragma:
        return SchemaVersion.from_user_version(pragma)
    business_tables = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
        "('schema_version','schema_migrations','schema_transitions')"
    ).fetchone()[0]
    if business_tables:
        raise MigrationError("non-empty database has no schema version")
    return None


def _metadata_ddl() -> str:
    return """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    major INTEGER NOT NULL CHECK (major >= 0),
    minor INTEGER NOT NULL CHECK (minor >= 0),
    database_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    major INTEGER NOT NULL,
    minor INTEGER NOT NULL,
    filename TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    PRIMARY KEY (major, minor),
    UNIQUE (filename)
);
CREATE TABLE IF NOT EXISTS schema_transitions (
    transition_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


def _verify_checksums(conn: sqlite3.Connection,
                      steps: list[MigrationStep]) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        return
    known = {(s.version.major, s.version.minor): s for s in steps}
    for major, minor, filename, checksum in conn.execute(
            "SELECT major,minor,filename,checksum FROM schema_migrations"):
        step = known.get((major, minor))
        if step and (step.path.name != filename or step.checksum != checksum):
            raise MigrationError(
                f"checksum mismatch for recorded migration V{major}.{minor}: "
                f"{filename}")


def _run_locked(db_path: str, schema_dir: str, database_name: str,
                target: SchemaVersion | None = None) -> None:
    conn = sqlite_runtime.connect(db_path, "schema_upgrade")
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        current = _database_version(conn, database_name)
        selected = _resolve_schema_dir(schema_dir, database_name, current)
        steps = _sql_steps(selected)
        target_major = steps[0].version.major
        if current is None and target_major >= 1:
            # Must be set before the runner creates metadata tables; changing
            # auto_vacuum after the first table would require a full VACUUM.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        if current is not None and current.major != target_major:
            raise MigrationError(
                f"major schema mismatch: database V{current.major}.{current.minor}, "
                f"files V{target_major}; use a transition tool")
        _verify_checksums(conn, steps)
        latest = steps[-1].version
        requested = target or latest
        if requested.major != target_major:
            raise MigrationError(
                f"requested target V{requested.major}.{requested.minor} "
                f"does not belong to {database_name} V{target_major}")
        if requested > latest:
            raise MigrationError(
                f"requested target V{requested.major}.{requested.minor} "
                f"is newer than known V{latest.major}.{latest.minor}")
        if current is not None and current > requested:
            raise MigrationError(
                f"database is already V{current.major}.{current.minor}, "
                f"cannot move backwards to V{requested.major}.{requested.minor}")
        if current is not None and current.minor > latest.minor:
            print(f"warning: {database_name} V{current.major}.{current.minor} "
                  f"is newer than known V{latest.major}.{latest.minor}; "
                  "continuing under same-major compatibility", file=sys.stderr)
            return

        for step in steps:
            if step.version > requested:
                break
            if current is not None and step.version <= current:
                continue
            if step.version.major != target_major:
                raise MigrationError("migration list changed major while running")
            body = step.path.read_text(encoding="utf-8")
            if not body.strip():
                raise MigrationError(f"empty migration: {step.path}")
            v = step.version
            sql = (
                "BEGIN IMMEDIATE;\n" + _metadata_ddl() + body + "\n"
                "INSERT INTO schema_migrations"
                "(major,minor,filename,checksum,applied_at) VALUES ("
                f"{v.major},{v.minor},{_quote(step.path.name)},"
                f"{_quote(step.checksum)},strftime('%Y-%m-%dT%H:%M:%fZ','now'));\n"
                "INSERT INTO schema_version(id,major,minor,database_name,updated_at) "
                f"VALUES(1,{v.major},{v.minor},{_quote(database_name)},"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(id) DO UPDATE SET major=excluded.major,"
                "minor=excluded.minor,database_name=excluded.database_name,"
                "updated_at=excluded.updated_at;\n"
                f"PRAGMA user_version={v.user_version};\nCOMMIT;"
            )
            try:
                conn.executescript(sql)
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # executescript may already have rolled the transaction
                    # back; preserve the original migration exception.
                    _rollback_was_already_complete = True
                raise
            current = v
    finally:
        conn.close()
