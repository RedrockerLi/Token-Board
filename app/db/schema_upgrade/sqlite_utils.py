"""Small SQLite file operations shared by upgrade paths."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def copy_sqlite(source: Path, destination: Path) -> None:
    """Copy a SQLite database with its WAL state through the backup API."""
    destination.unlink(missing_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
