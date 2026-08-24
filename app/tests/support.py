from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
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
