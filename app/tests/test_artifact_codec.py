"""Tests for the streaming SQLite artifact codec."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.sync import artifact_codec


class ArtifactCodecTest(unittest.TestCase):
    @staticmethod
    def _sqlite_source(path: Path) -> bytes:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
            conn.executemany(
                "INSERT INTO values_table(value) VALUES(?)",
                [(f"value-{index}",) for index in range(32)],
            )
            conn.commit()
        finally:
            conn.close()
        return path.read_bytes()

    def test_gzip_round_trip_is_deterministic_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            original = self._sqlite_source(source)
            digest = hashlib.sha256(original).hexdigest()
            encoded_bytes = []
            destination = root / "decoded.db"

            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                encoded_bytes.append(encoded.read_bytes())
                self.assertEqual(encoded.read_bytes()[:2], b"\x1f\x8b")
                artifact_codec.decode_sqlite_artifact(
                    encoded, "token-board_config_20260825_010203.db.gz",
                    destination,
                )
                self.assertEqual(
                    hashlib.sha256(destination.read_bytes()).hexdigest(), digest)
            self.assertFalse(encoded.exists())

            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                encoded_bytes.append(encoded.read_bytes())
            self.assertEqual(encoded_bytes[0], encoded_bytes[1])

    def test_legacy_database_payload_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            original = self._sqlite_source(source)
            payload = root / "legacy.db"
            payload.write_bytes(original)
            destination = root / "decoded.db"

            artifact_codec.decode_sqlite_artifact(
                payload, "dashboard_sync_20260825_010203.db", destination)
            self.assertEqual(destination.read_bytes(), original)

    def test_corrupt_payload_leaves_destination_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            self._sqlite_source(source)
            payload = root / "broken.db.gz"
            destination = root / "destination.db"
            destination.write_bytes(b"existing destination")

            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                payload.write_bytes(encoded.read_bytes()[:-8])
            with self.assertRaises(artifact_codec.ArtifactCodecError):
                artifact_codec.decode_sqlite_artifact(
                    payload, "token-board_config_20260825_010203.db.gz",
                    destination,
                )
            self.assertEqual(destination.read_bytes(), b"existing destination")

    def test_bad_crc_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            self._sqlite_source(source)
            payload = root / "bad-crc.db.gz"
            destination = root / "destination.db"
            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                corrupted = bytearray(encoded.read_bytes())
            corrupted[-8] ^= 0x01
            payload.write_bytes(corrupted)

            with self.assertRaises(artifact_codec.ArtifactCodecError):
                artifact_codec.decode_sqlite_artifact(
                    payload, "token-board_config_20260825_010203.db.gz",
                    destination)
            self.assertFalse(destination.exists())

    def test_non_sqlite_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = root / "not-a-database.db"
            destination = root / "destination.db"
            payload.write_bytes(b"not a sqlite database")

            with self.assertRaises(artifact_codec.ArtifactCodecError):
                artifact_codec.decode_sqlite_artifact(
                    payload, "dashboard_sync_20260825_010203.db", destination)
            self.assertFalse(destination.exists())

    def test_decompressed_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            self._sqlite_source(source)
            destination = root / "destination.db"
            with artifact_codec.encode_sqlite_artifact(source) as encoded:
                payload = root / "payload.db.gz"
                payload.write_bytes(encoded.read_bytes())

            with patch.object(artifact_codec, "MAX_DECOMPRESSED_BYTES", 16):
                with self.assertRaises(artifact_codec.ArtifactCodecError):
                    artifact_codec.decode_sqlite_artifact(
                        payload, "dashboard_sync_20260825_010203.db.gz",
                        destination)
            self.assertFalse(destination.exists())

    def test_copy_uses_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.db"
            source.write_bytes(b"SQLite format 3\x00" + b"x" * (128 * 1024))
            destination = root / "destination.db"
            with patch.object(artifact_codec, "CHUNK_SIZE", 17):
                with artifact_codec.encode_sqlite_artifact(source) as encoded:
                    artifact_codec.decode_sqlite_artifact(
                        encoded, "dashboard_sync_20260825_010203.db.gz",
                        destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
