"""Versioned SQLite schema migrations (single source of truth).

All DDL for a database lives in ``schema/<db-name>/NNNN_desc.sql`` files.
This runner applies, in ascending order, every migration whose number is
greater than the database's ``PRAGMA user_version``, bumping that version in
the SAME transaction as the migration body — so each step is all-or-nothing.

Both the C++ proxy (``proxy/src/db.cpp::run_migrations``) and this Python
module implement the same protocol and share the same ``schema/`` files, so
either process can bootstrap or upgrade a database.  An advisory flock on
``<db_path>.migrate.lock`` serializes concurrent runners across processes;
the ``BEGIN IMMEDIATE`` inside each step serializes against any other writer.

Transaction control is owned by the runner — migration files must NOT contain
``BEGIN``/``COMMIT``/``PRAGMA user_version``.
"""

import fcntl
import os
import sqlite3
from pathlib import Path


class MigrationError(RuntimeError):
    """Raised when the schema directory is unusable or a step fails."""


def schema_dir_for(db_path: str, name: str) -> str:
    """Derive the migration directory for a database.

    ``data/proxy.db`` → ``<repo>/schema/proxy``; ``data/dashboard.db`` →
    ``<repo>/schema/dashboard``.
    """
    return str(Path(db_path).resolve().parent.parent / "schema" / name)


def migrate(db_path: str, schema_dir: str) -> None:
    """Apply pending migrations to ``db_path`` (no-op when up to date).

    Raises :class:`MigrationError` (or ``sqlite3.Error`` from a failing step)
    so callers can fail fast instead of running against a half-migrated schema.
    """
    fd = os.open(db_path + ".migrate.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # pairs with the C++ proxy's flock()
        _run_locked(db_path, schema_dir)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sql_steps(schema_dir: str) -> list[tuple[int, Path]]:
    """Enumerate and validate ``schema_dir/NNNN_*.sql`` in ascending order."""
    d = Path(schema_dir)
    if not d.is_dir():
        raise MigrationError(f"schema dir not found: {schema_dir}")

    steps: list[tuple[int, Path]] = []
    for p in sorted(d.glob("*.sql")):
        num = p.stem.split("_", 1)[0]
        if not (num.isdigit() and len(num) == 4):
            raise MigrationError(
                f"bad migration filename: {p.name} (need NNNN_desc.sql)")
        steps.append((int(num), p))

    if not steps:
        raise MigrationError(f"no migration files in {schema_dir}")

    nums = [n for n, _ in steps]
    if len(set(nums)) != len(nums):
        raise MigrationError(f"duplicate migration numbers in {schema_dir}")
    return steps


def _run_locked(db_path: str, schema_dir: str) -> None:
    steps = _sql_steps(schema_dir)

    # isolation_level=None = autocommit: executescript won't auto-commit any
    # pending transaction (there is none), so our explicit BEGIN IMMEDIATE …
    # COMMIT runs as one atomic transaction.
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        for n, p in steps:
            if n <= version:
                continue
            script = (f"BEGIN IMMEDIATE;\n{p.read_text(encoding='utf-8')}\n"
                      f"PRAGMA user_version = {n};\nCOMMIT;")
            try:
                conn.executescript(script)
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")  # step failed → restore old version
                except sqlite3.Error:
                    pass
                raise  # fail-fast; caller propagates
    finally:
        conn.close()  # closes abort any open transaction as a safety net
