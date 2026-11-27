from __future__ import annotations

import os
import sqlite3
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def short_temp_root() -> tempfile.TemporaryDirectory:
    """Temporary root on a short base directory.

    AF_UNIX ``sun_path`` is 108 bytes, so the maintenance socket — which stays
    at the production location ``<root>/data/token-maintenance.sock`` — cannot
    be bound when ``TMPDIR`` is deep.  The databases and the socket layout keep
    their normal shape; only the base of the temporary tree is short.
    """
    for base in ("/tmp", "/var/tmp"):
        try:
            if os.path.isdir(base) and os.access(base, os.W_OK | os.X_OK):
                return tempfile.TemporaryDirectory(dir=base)
        except OSError:
            continue
    return tempfile.TemporaryDirectory()


@contextmanager
def sqlite_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Own a test connection and make its transaction outcome explicit."""
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


class AppDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = short_temp_root()
        self.root = Path(self._temporary.name)
        shutil.copytree(PROJECT_ROOT / "schema", self.root / "schema")
        (self.root / "data").mkdir()
        self.proxy_path = self.root / "data/token-board.db"
        self.dashboard_path = self.root / "data/dashboard.db"
        from app.db.schema_upgrade import ensure_local_databases
        ensure_local_databases(
            str(self.proxy_path), str(self.dashboard_path),
            self.root / "schema")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def proxy_database(self):
        from app.db.proxy_db import ProxyDatabase
        return ProxyDatabase(str(self.proxy_path))
