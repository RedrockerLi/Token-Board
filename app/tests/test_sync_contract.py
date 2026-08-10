from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.migrations import migrate
from app.db.schema_upgrade.coordinator import inspect_version
from app.services.sync.settings import SyncConfig, save_sync_config
from app.services.sync.webdav import (
    RemoteArtifact,
    WebDAVConflict,
    _upload_versioned_artifact,
)
from app.services.sync.config_sync import sync_config_upload
from app.services.sync.dashboard_sync import sync_dashboard

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SyncContractTest(unittest.TestCase):
    def test_versioned_upload_refuses_stale_remote_etag(self) -> None:
        config = SyncConfig("https://dav.example", "token-board", "u", "p")
        expected = RemoteArtifact("proxy_config_20260809_010000.db", etag='"old"')
        current = RemoteArtifact("proxy_config_20260809_010001.db", etag='"new"')
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "config.db"
            source.write_bytes(b"fixture")
            with patch("app.services.sync.webdav._latest_artifact",
                       return_value=current), patch(
                           "app.services.sync.webdav._webdav_upload") as upload:
                with self.assertRaises(WebDAVConflict):
                    _upload_versioned_artifact(config, str(source), "proxy_config",
                                               expected)
                upload.assert_not_called()

    def test_dashboard_retries_complete_transaction_after_upload_race(self) -> None:
        with patch("app.services.sync.dashboard_sync._sync_dashboard_once",
                   side_effect=[WebDAVConflict("race-1"),
                                WebDAVConflict("race-2"),
                                {"status": "ok"}]) as once:
            result = sync_dashboard("proxy.db", "dashboard.db")
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(once.call_count, 3)

    def test_config_upload_retries_complete_transaction_after_upload_race(self) -> None:
        with patch("app.services.sync.config_sync._sync_config_upload_once",
                   side_effect=[WebDAVConflict("race-1"),
                                WebDAVConflict("race-2"),
                                {"status": "ok"}]) as once:
            result = sync_config_upload("proxy.db")
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(once.call_count, 3)

    def _repo_layout(self) -> tuple[str, str, str]:
        """Temp dir laid out like the repo (schema/ + data/) so that
        ``schema_dir_for`` resolves to a real schema root."""
        temp = tempfile.mkdtemp()
        shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
        (Path(temp) / "data").mkdir()
        proxy = str(Path(temp) / "data" / "proxy.db")
        dash = str(Path(temp) / "data" / "dashboard.db")
        migrate(proxy, str(Path(temp) / "schema"), "proxy")
        migrate(dash, str(Path(temp) / "schema"), "dashboard")
        save_sync_config(proxy, SyncConfig(
            "https://dav.example/remote.php/dav/files/u",
            "token-board-sync", "user", "pass"))
        return temp, proxy, dash

    def _webdav_mocks(self):
        """Disable the network for a full local sync transaction.

        The sync package's ``__init__`` copies shared helpers into every
        module namespace, so a function like ``_latest_artifact`` must be
        patched both where the caller looks it up (config_sync /
        dashboard_sync) and where webdav's own helpers look it up.
        """
        for module in ("app.services.sync.webdav",
                       "app.services.sync.config_sync",
                       "app.services.sync.dashboard_sync"):
            patch(module + "._latest_artifact", return_value=None).start()
            patch(module + "._webdav_download", return_value=False).start()
            patch(module + "._webdav_upload", return_value=None).start()

    def test_config_upload_records_remote_metadata(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            self._webdav_mocks()
            result = sync_config_upload(proxy)
            self.assertEqual(result["status"], "ok", result)
            conn = sqlite3.connect(proxy)
            rows = dict(conn.execute(
                "SELECT key, value FROM sync_state").fetchall())
            conn.close()
            self.assertIn("proxy_remote_sha256", rows)
            self.assertEqual(len(rows["proxy_remote_sha256"]), 64)
            self.assertEqual(rows["proxy_remote_major"], "1")
            version = inspect_version(Path(proxy), "proxy")
            self.assertEqual(rows["proxy_remote_minor"], str(version.minor))
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_dashboard_sync_records_remote_metadata(self) -> None:
        temp, proxy, dash = self._repo_layout()
        try:
            self._webdav_mocks()
            result = sync_dashboard(proxy, dash)
            self.assertEqual(result["status"], "ok", result)
            conn = sqlite3.connect(proxy)
            rows = dict(conn.execute(
                "SELECT key, value FROM sync_state").fetchall())
            conn.close()
            self.assertIn("dashboard_remote_sha256", rows)
            self.assertEqual(len(rows["dashboard_remote_sha256"]), 64)
            self.assertEqual(rows["dashboard_remote_major"], "1")
            version = inspect_version(Path(dash), "dashboard")
            self.assertEqual(rows["dashboard_remote_minor"], str(version.minor))
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)
