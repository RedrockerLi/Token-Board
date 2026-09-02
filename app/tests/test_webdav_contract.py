"""Contract tests for the explicit WebDAV transport/transaction boundary."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.sync.settings import SyncConfig
from app.services.sync.webdav import (
    ArtifactTransaction,
    RemoteArtifact,
    WebDAVClient,
    WebDAVConflict,
    WebDAVError,
)


class _MemoryClient(WebDAVClient):
    def __init__(self):
        super().__init__(SyncConfig("https://dav.example", "sync", "u", "p"))
        self.items: list[RemoteArtifact] = []
        self.uploaded: list[str] = []

    def list_artifacts(self, prefix: str):
        return [item for item in self.items if item.name.startswith(prefix)]

    def upload_artifact(self, source: str, name: str, **kwargs):
        self.uploaded.append(name)
        if any(item.name == name for item in self.items):
            raise WebDAVConflict("collision")
        self.items.append(RemoteArtifact(name, etag=f'"{len(self.uploaded)}"'))


class WebDAVContractTest(unittest.TestCase):
    def test_publish_requires_post_put_listing_confirmation(self):
        client = _MemoryClient()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            transaction = ArtifactTransaction(
                client,
                filename_clock=lambda: datetime(2026, 8, 25, 1, 2, 3),
            )
            result = transaction.publish_versioned_artifact(
                str(source), "dashboard_sync")
            self.assertEqual(result.name, "dashboard_sync_20260825_010203.db")
            self.assertEqual(client.uploaded, [result.name])

    def test_stale_expected_artifact_is_conflict_before_put(self):
        client = _MemoryClient()
        client.items.append(RemoteArtifact(
            "token-board_config_20260825_010000.db", etag='"new"'))
        transaction = ArtifactTransaction(
            client, filename_clock=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            with self.assertRaises(WebDAVConflict):
                transaction.publish_versioned_artifact(
                    str(source), "token-board_config",
                    RemoteArtifact(
                        "token-board_config_20260825_010000.db", etag='"old"'))
        self.assertEqual(client.uploaded, [])

    def test_missing_confirmation_is_not_a_success(self):
        class NoConfirm(_MemoryClient):
            def list_artifacts(self, prefix: str):
                return []

        client = NoConfirm()
        transaction = ArtifactTransaction(
            client, filename_clock=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc))
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            with self.assertRaises(WebDAVError):
                transaction.publish_versioned_artifact(str(source), "config")

    def test_config_publish_accepts_put_without_listing_confirmation(self):
        from app.services.sync import webdav

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            calls = []

            def put(config, path, remote_filename=None, **kwargs):
                calls.append(remote_filename)

            with patch.object(webdav, "upload_artifact", side_effect=put), \
                    patch.object(webdav, "list_artifacts",
                                 side_effect=AssertionError("must not list")):
                result = webdav.publish_config_artifact(
                    SyncConfig("https://dav.example", "sync", "u", "p"),
                    str(source),
                )
            self.assertEqual(result.name, calls[0])
            self.assertTrue(result.name.startswith("token-board_config_"))

    def test_retry_uses_a_fresh_artifact_name(self):
        class RetryClient(_MemoryClient):
            def list_artifacts(self, prefix: str):
                return []

        client = RetryClient()
        transaction = ArtifactTransaction(
            client, filename_clock=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
            retry_count=2)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            with self.assertRaises(WebDAVError):
                transaction.publish_with_retry(str(source), "config")
        self.assertEqual(client.uploaded,
                         ["config_20260825_000000.db",
                          "config_20260825_000000_1.db"])


if __name__ == "__main__":
    unittest.main()
