from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app.services.logging_setup import configure_logging, reset_logging


class LoggingSetupTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_logging()

    def test_file_handler_records_both_levels_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            configure_logging(Path(tmp))
            logger = logging.getLogger("app.test.logging")
            logger.info("info-marker")
            logger.warning("warning-marker")
            for handler in logging.getLogger().handlers:
                handler.flush()

            path = Path(tmp) / "dashboard.log"
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("INFO app.test.logging: info-marker", text)
            self.assertIn("WARNING app.test.logging: warning-marker", text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_configure_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            configure_logging(Path(tmp))
            handler_count = len(logging.getLogger().handlers)
            configure_logging(Path(tmp))
            self.assertEqual(len(logging.getLogger().handlers), handler_count)


if __name__ == "__main__":
    unittest.main()
