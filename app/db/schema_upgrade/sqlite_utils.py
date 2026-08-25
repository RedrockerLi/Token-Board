"""Small SQLite file operations shared by upgrade paths."""

from __future__ import annotations

from pathlib import Path

from app.core import sqlite_runtime


def copy_sqlite(source: Path, destination: Path) -> None:
    """Copy a SQLite database with its WAL state through the backup API."""
    destination.unlink(missing_ok=True)
    src = sqlite_runtime.connect(source, "schema_upgrade")
    dst = sqlite_runtime.connect(destination, "schema_upgrade")
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
