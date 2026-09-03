"""Contract tests for the explicit WebDAV transport/transaction boundary."""

import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services.sync import artifact_codec, webdav
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
        self.uploaded_payloads: list[bytes] = []

    def list_artifacts(self, prefix: str):
        return [item for item in self.items if item.name.startswith(prefix)]

    def upload_artifact(self, source: str, name: str, **kwargs):
        self.uploaded.append(name)
        self.uploaded_payloads.append(Path(source).read_bytes())
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
            self.assertEqual(result.name, "dashboard_sync_20260825_010203.db.gz")
            self.assertEqual(client.uploaded, [result.name])
            self.assertEqual(client.uploaded_payloads[0][:2], b"\x1f\x8b")

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
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "artifact.db"
            source.write_bytes(b"fixture")
            calls = []

            def put(config, path, remote_filename=None, **kwargs):
                calls.append((remote_filename, Path(path).read_bytes()))

            with patch.object(webdav, "upload_artifact", side_effect=put), \
                    patch.object(webdav, "list_artifacts",
                                 side_effect=AssertionError("must not list")):
                result = webdav.publish_config_artifact(
                    SyncConfig("https://dav.example", "sync", "u", "p"),
                    str(source),
                )
            self.assertEqual(result.name, calls[0][0])
            self.assertTrue(result.name.startswith("token-board_config_"))
            self.assertTrue(result.name.endswith(".db.gz"))
            self.assertEqual(gzip.decompress(calls[0][1]), b"fixture")

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
                         ["config_20260825_000000.db.gz",
                          "config_20260825_000000_1.db.gz"])

    def test_latest_prefers_gzip_for_the_same_timestamp(self):
        items = [
            RemoteArtifact("token-board_config_20260825_010203.db"),
            RemoteArtifact("token-board_config_20260825_010203.db.gz"),
        ]
        with patch.object(webdav, "list_artifacts", return_value=items):
            result = webdav.latest_artifact(
                SyncConfig("https://dav.example", "sync", "u", "p"),
                "token-board_config",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.name, "token-board_config_20260825_010203.db.gz")

    @staticmethod
    def _response(payload: bytes):
        class Response:
            status_code = 200
            ok = True

            def iter_content(self, chunk_size):
                del chunk_size
                for offset in range(0, len(payload), 7):
                    yield payload[offset:offset + 7]

            def close(self):
                self.closed = True

        return Response()

    def test_download_decodes_gzip_stream_and_preserves_sqlite_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            source.write_bytes(b"SQLite format 3\x00source")
            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                payload = encoded.read_bytes()
            destination = root / "destination.db"
            response = self._response(payload)
            config = SyncConfig("https://dav.example", "sync", "u", "p")
            with patch.object(webdav.requests, "get", return_value=response) as get:
                self.assertTrue(webdav.download_artifact(
                    config, str(destination),
                    remote_filename="token-board_config_20260825_010203.db.gz"))
            get.assert_called_once()
            args, kwargs = get.call_args
            self.assertEqual(
                args[0],
                "https://dav.example/sync/token-board_config_20260825_010203.db.gz")
            self.assertIsInstance(kwargs["auth"], webdav.HTTPBasicAuth)
            self.assertEqual(kwargs["timeout"], 30)
            self.assertTrue(kwargs["stream"])
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_download_accepts_legacy_uncompressed_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "destination.db"
            payload = b"SQLite format 3\x00legacy"
            response = self._response(payload)
            config = SyncConfig("https://dav.example", "sync", "u", "p")
            with patch.object(webdav.requests, "get", return_value=response):
                self.assertTrue(webdav.download_artifact(
                    config, str(destination),
                    remote_filename="dashboard_sync_20260825_010203.db"))
            self.assertEqual(destination.read_bytes(), payload)

    def test_corrupt_download_does_not_replace_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "destination.db"
            destination.write_bytes(b"old")
            response = self._response(b"not gzip")
            config = SyncConfig("https://dav.example", "sync", "u", "p")
            with patch.object(webdav.requests, "get", return_value=response):
                with self.assertRaises(WebDAVError):
                    webdav.download_artifact(
                        config, str(destination),
                        remote_filename="dashboard_sync_20260825_010203.db.gz")
            self.assertEqual(destination.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
