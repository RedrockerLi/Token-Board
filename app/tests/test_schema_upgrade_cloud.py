"""Cloud-artifact upgrade contract tests (using local shadow files)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.migrations import migrate
from app.db.schema_upgrade import upgrade_shadow


class CloudSchemaUpgradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                        self.root / "schema")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_downloaded_v0_artifact_is_upgraded_in_place_without_source(self) -> None:
        source = self.root / "source-v0.db"
        migrate(str(source), str(self.root / "schema/proxy/v0"), "proxy")
        with sqlite3.connect(source) as conn:
            conn.execute(
                "INSERT INTO upstream_accounts(name,base_url,account_type) "
                "VALUES('cloud-old','https://old.example','api')")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        remote = self.root / "downloaded-artifact.db"
        shutil.copy2(source, remote)
        result = upgrade_shadow(str(remote), "proxy", self.root / "schema")
        self.assertEqual(result.current.major, 1)
        self.assertEqual(sqlite3.connect(source).execute(
            "PRAGMA user_version").fetchone()[0], 19)
        with sqlite3.connect(remote) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                             max(int(path.name.split("_", 1)[0].split("-")[0]) * 10000
                                 + int(path.name.split("_", 1)[0].split("-")[1])
                                 for path in (self.root / "schema/proxy/v1").glob("*.sql")))
            self.assertEqual(conn.execute(
                "SELECT name FROM accounts WHERE name='cloud-old'").fetchone()[0],
                             "cloud-old")

    def test_configuration_upgrade_drops_remote_runtime_state(self) -> None:
        source = self.root / "source-v0-full.db"
        migrate(str(source), str(self.root / "schema/proxy/v0"), "proxy")
        with sqlite3.connect(source) as conn:
            conn.execute(
                "INSERT INTO upstream_accounts(name,base_url,account_type) "
                "VALUES('cloud-config','https://old.example','api')")
            account_id = conn.execute(
                "SELECT id FROM upstream_accounts WHERE name='cloud-config'").fetchone()[0]
            conn.execute(
                "INSERT INTO request_log(account_id,model,status_code) "
                "VALUES(?,?,200)", (account_id, "old-model"))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        remote = self.root / "downloaded-config.db"
        shutil.copy2(source, remote)
        upgrade_shadow(str(remote), "proxy", self.root / "schema",
                       configuration_only=True)
        with sqlite3.connect(remote) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM request_log").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT name FROM accounts WHERE name='cloud-config'").fetchone()[0],
                "cloud-config")


if __name__ == "__main__":
    unittest.main()
