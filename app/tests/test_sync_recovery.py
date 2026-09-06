"""Crash/recovery fixtures for the explicit sync commit protocol."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.schema_upgrade import ensure_local_databases
from app.services.sync import config_sync, dashboard_sync
from app.services.sync.settings import SyncConfig, save_sync_config
from app.services.sync.state import get_sync_state, set_sync_state_many
from app.services.sync.webdav import RemoteArtifact


_REPO_ROOT = Path(__file__).resolve().parents[2]


class SyncRecoveryTest(unittest.TestCase):
    def _repo_layout(self) -> tuple[str, str, str]:
        temp = tempfile.mkdtemp()
        shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
        (Path(temp) / "data").mkdir()
        proxy = str(Path(temp) / "data" / "token-board.db")
        dash = str(Path(temp) / "data" / "dashboard.db")
        ensure_local_databases(proxy, dash, Path(temp) / "schema")
        save_sync_config(proxy, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))
        return temp, proxy, dash

    def test_deleted_pending_remote_is_republished_and_reconcile_is_idempotent(self):
        temp, proxy, dash = self._repo_layout()
        try:
            pending = dashboard_sync._pending_dashboard_path(dash)
            shutil.copy2(dash, pending)
            set_sync_state_many(proxy, {
                "dashboard_pending_path": pending,
                "dashboard_pending_export_max_id": "0",
                "dashboard_pending_remote_artifact":
                    "dashboard_sync_20260825_010000.db",
                "dashboard_pending_remote_etag": '"old"',
            })
            config = SyncConfig(
                "https://dav.example/remote", "token-board-sync", "user", "pass")
            replacement = RemoteArtifact(
                "dashboard_sync_20260825_010001.db", etag='"new"')
            with patch.object(dashboard_sync, "find_artifact", return_value=None), \
                    patch.object(dashboard_sync, "latest_artifact", return_value=None), \
                    patch.object(dashboard_sync, "publish_versioned_artifact",
                                 return_value=replacement) as publish, \
                    patch.object(dashboard_sync, "publish_schema_manifest"):
                result = dashboard_sync._recover_dashboard_pending(
                    proxy, dash, config, str(Path(temp) / "schema"))
                self.assertEqual(result["status"], "ok", result)
                second = dashboard_sync._recover_dashboard_pending(
                    proxy, dash, config, str(Path(temp) / "schema"))

            self.assertTrue(result["recovered"])
            self.assertIsNone(second)
            publish.assert_called_once()
            self.assertFalse(os.path.exists(pending))
            self.assertIsNone(get_sync_state(proxy, "dashboard_pending_path"))
            self.assertEqual(
                get_sync_state(proxy, "dashboard_remote_artifact"),
                replacement.name)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_missing_prepared_export_keeps_pending_and_checkpoint_unchanged(self):
        temp, proxy, dash = self._repo_layout()
        try:
            missing = str(Path(temp) / "data" / ".dashboard.pending.db")
            set_sync_state_many(proxy, {
                "dashboard_pending_path": missing,
                "dashboard_pending_export_max_id": "17",
            })
            config = SyncConfig(
                "https://dav.example/remote", "token-board-sync", "user", "pass")
            result = dashboard_sync._recover_dashboard_pending(
                proxy, dash, config, str(Path(temp) / "schema"))
            self.assertEqual(result["status"], "error")
            self.assertTrue(result["pending"])
            self.assertEqual(get_sync_state(proxy, "dashboard_pending_export_max_id"),
                             "17")
            self.assertIsNone(get_sync_state(proxy, "last_exported_log_id"))
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_pending_is_discarded_before_startup_pull(self):
        temp, proxy, _ = self._repo_layout()
        try:
            pending = config_sync._pending_config_path(proxy)
            shutil.copy2(proxy, pending)
            set_sync_state_many(proxy, {
                "config_pending_path": pending,
                "config_pending_remote_artifact": "old.db",
                "config_pending_remote_etag": '"old"',
            })
            config = SyncConfig(
                "https://dav.example/remote", "token-board-sync", "user", "pass")
            with patch.object(config_sync, "latest_artifact", return_value=None):
                result = config_sync.sync_config_pull(
                    proxy, str(Path(temp) / "schema"), config=config)

            self.assertEqual(result["status"], "empty", result)
            self.assertFalse(os.path.exists(pending))
            self.assertIsNone(get_sync_state(proxy, "config_pending_path"))
            self.assertIsNone(get_sync_state(proxy, "config_pending_remote_artifact"))
        finally:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
