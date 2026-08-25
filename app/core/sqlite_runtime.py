"""Explicit SQLite connection profiles and transaction ownership.

Profiles describe behavior that used to be copied at many call sites.  The
``transaction`` context manager deliberately does not close its connection:
the caller that acquired a connection owns its lifetime.  It also refuses an
implicit nested ``BEGIN``; callers that need nested rollback scope must use an
explicit SQLite savepoint.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SQLiteProfile:
    name: str
    timeout: float = 5.0
    busy_timeout_ms: int = 5000
    row_factory: object | None = sqlite3.Row
    journal_mode: str | None = "WAL"
    foreign_keys: bool = True
    read_only: bool = False
    isolation_level: str | None = ""


PROFILES: dict[str, SQLiteProfile] = {
    "proxy_runtime": SQLiteProfile("proxy_runtime", timeout=10.0),
    "dashboard_runtime": SQLiteProfile("dashboard_runtime", timeout=10.0),
    "billing_write": SQLiteProfile("billing_write", timeout=10.0),
    "shadow_copy": SQLiteProfile("shadow_copy", timeout=10.0),
    "snapshot_restore": SQLiteProfile(
        "snapshot_restore", timeout=10.0, journal_mode="DELETE",
        foreign_keys=False),
    # The migration engine chooses WAL explicitly while applying SQL.  Read
    # and verification steps must not change a database's journal mode merely
    # by inspecting it.
    "schema_upgrade": SQLiteProfile(
        "schema_upgrade", timeout=10.0, journal_mode=None,
        foreign_keys=False, isolation_level=None),
    "agent_external": SQLiteProfile(
        "agent_external", timeout=2.0, busy_timeout_ms=2000,
        journal_mode=None, foreign_keys=False, read_only=True),
}


def _select_profile(name: str | SQLiteProfile) -> SQLiteProfile:
    if isinstance(name, SQLiteProfile):
        return name
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown sqlite runtime profile: {name}") from exc


def profile(name: str | SQLiteProfile) -> SQLiteProfile:
    return _select_profile(name)


def _readonly_uri(path: str | Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return f"file:{resolved.as_posix()}?mode=ro"


def connect(path: str | Path, profile: str | SQLiteProfile = "proxy_runtime",
            *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open *path* under an explicit profile and apply its real PRAGMAs."""

    selected = profile if isinstance(profile, SQLiteProfile) else _select_profile(profile)
    target = _readonly_uri(path) if selected.read_only else str(path)
    conn = sqlite3.connect(
        target, uri=selected.read_only, timeout=selected.timeout,
        isolation_level=selected.isolation_level,
        check_same_thread=check_same_thread)
    if selected.row_factory is not None:
        conn.row_factory = selected.row_factory
    conn.execute(f"PRAGMA busy_timeout={int(selected.busy_timeout_ms)}")
    if selected.foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn.execute("PRAGMA foreign_keys=OFF")
    if selected.journal_mode is not None and not selected.read_only:
        # SQLite may return a different mode for :memory: databases; the
        # caller/tests can inspect the actual result rather than trusting the
        # profile label.
        conn.execute(f"PRAGMA journal_mode={selected.journal_mode}")
    return conn


def read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing database without write capability."""

    return connect(path, "agent_external")


@contextmanager
def connection(path: str | Path,
               profile: str | SQLiteProfile = "proxy_runtime") -> Iterator[sqlite3.Connection]:
    """Own a connection for one operation and close it at the boundary."""

    conn = connect(path, profile)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "deferred") -> Iterator[sqlite3.Connection]:
    """Begin/commit/rollback a transaction without taking connection ownership."""

    if conn.in_transaction:
        raise RuntimeError(
            "transaction() refuses an implicit nested BEGIN; use an explicit SAVEPOINT")
    normalized = mode.lower()
    if normalized not in {"deferred", "immediate", "exclusive"}:
        raise ValueError("transaction mode must be deferred, immediate, or exclusive")
    conn.execute("BEGIN" if normalized == "deferred" else f"BEGIN {normalized.upper()}")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = [
    "PROFILES", "SQLiteProfile", "connect", "connection", "profile",
    "read_only", "transaction",
]
