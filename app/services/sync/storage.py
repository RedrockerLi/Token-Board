"""Local shadow-copy operations used by both sync domains."""

from app.core import sqlite_runtime


def safe_copy_db(source: str, destination: str) -> None:
    """Copy a SQLite database including WAL data through the backup API."""
    source_conn = sqlite_runtime.connect(source, "shadow_copy")
    destination_conn = sqlite_runtime.connect(destination, "shadow_copy")
    try:
        source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
