"""Contracts for the manual dashboard configuration session."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.migrations import migrate
from app import create_app
from app.services.sync.config_session import ConfigSession


class ConfigSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        root = Path(self.temp)
        shutil.copytree(Path(__file__).resolve().parents[2] / "schema", root / "schema")
        (root / "data").mkdir()
        self.db = root / "data" / "token-board.db"
        self.schema = root / "schema"
        migrate(str(self.db), str(self.schema), "token-board")

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def _wait(self, session):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if session.status().state != "syncing":
                return session.status()
            time.sleep(0.01)
        return session.status()

    def test_pull_success_unlocks_and_calls_callback(self):
        ready = threading.Event()
        with patch("app.services.sync.config_session.load_sync_config", return_value=object()), \
                patch("app.services.sync.config_session.sync_config_pull",
                      return_value={"status": "pulled", "message": "ok"}):
            session = ConfigSession(str(self.db), str(self.schema),
                                    on_writable=ready.set)
            session.start()
            self.assertEqual(self._wait(session).state, "writable")
            self.assertTrue(ready.wait(1))

    def test_pull_failure_stays_read_only(self):
        with patch("app.services.sync.config_session.load_sync_config", return_value=object()), \
                patch("app.services.sync.config_session.sync_config_pull",
                      return_value={"status": "error", "message": "offline"}):
            session = ConfigSession(str(self.db), str(self.schema))
            session.start()
            status = self._wait(session)
            self.assertEqual(status.state, "read_only")
            self.assertFalse(status.writable)
            self.assertEqual(status.message, "offline")

    def test_read_only_config_mutation_is_rejected_by_backend(self):
        dashboard = self.db.parent / "dashboard.db"
        migrate(str(dashboard), str(self.schema), "dashboard")
        app = create_app(str(self.db), schema_dir=str(self.schema),
                         testing=True, start_background_tasks=False)
        app.config["CONFIG_SESSION"]._set_state("read_only", "offline")
        response = app.test_client().post("/api/proxy/accounts", json={"name": "x"})
        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.get_json()["status"], "read_only")


if __name__ == "__main__":
    unittest.main()
