"""Cross-database V1 transitions never publish a half-upgraded pair."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.migrations import SchemaVersion, apply_sql_migrations
from app.db.schema_upgrade import ensure_local_databases


class CompoundSchemaUpgradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                        self.root / "schema")
        (self.root / "data").mkdir()
        self.proxy = self.root / "data/token-board.db"
        self.dashboard = self.root / "data/dashboard.db"
        apply_sql_migrations(str(self.proxy), str(self.root / "schema"), "token-board")
        apply_sql_migrations(
            str(self.dashboard), str(self.root / "schema"), "dashboard",
            SchemaVersion(1, 3))
        with sqlite3.connect(self.proxy) as conn:
            conn.execute(
                "INSERT INTO agent_software(id,uuid,name,agent_kind) "
                "VALUES(100,'agent-uuid','codex','codex')")
        with sqlite3.connect(self.dashboard) as conn:
            conn.execute(
                "INSERT INTO agent_software(software_id,name,agent_kind) "
                "VALUES(1,'codex','codex')")
            conn.execute(
                "INSERT INTO agent_daily_usage(date,software_id,model) "
                "VALUES('2026-01-01',1,'model')")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_identity_transition_publishes_both_sides_and_marker(self) -> None:
        result = ensure_local_databases(
            str(self.proxy), str(self.dashboard), self.root / "schema")
        self.assertIsNotNone(result["token-board"].manifest)
        with sqlite3.connect(self.dashboard) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT account_id FROM accounts WHERE account_kind='agent'"
                ).fetchone()[0], 100)
            marker = conn.execute(
                "SELECT checksum,generation_id FROM schema_transitions "
                "WHERE transition_id='v1-agent-identity'"
            ).fetchone()
        with sqlite3.connect(self.proxy) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 10010)
            proxy_marker = conn.execute(
                "SELECT checksum,generation_id FROM schema_transitions "
                "WHERE transition_id='v1-agent-identity'"
            ).fetchone()
        self.assertEqual(marker, proxy_marker)

    def test_publish_failure_restores_original_pair(self) -> None:
        original = (self.proxy.read_bytes(), self.dashboard.read_bytes())
        import app.db.schema_upgrade.compound as compound

        real_replace = compound.replace
        calls = 0

        def fail_on_dashboard(source: Path, shadow: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected dashboard publish failure")
            real_replace(source, shadow)

        with patch.object(compound, "replace", fail_on_dashboard):
            with self.assertRaises(RuntimeError):
                ensure_local_databases(
                    str(self.proxy), str(self.dashboard), self.root / "schema")
        self.assertEqual((self.proxy.read_bytes(), self.dashboard.read_bytes()), original)
        manifests = sorted(self.root.joinpath("data").glob(
            "auto-v1-compound-*.manifest.json"))
        self.assertEqual(
            json.loads(manifests[-1].read_text(encoding="utf-8"))["stage"],
            "recovered_rollback")


if __name__ == "__main__":
    unittest.main()
