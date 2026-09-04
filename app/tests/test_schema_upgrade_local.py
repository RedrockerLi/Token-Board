"""Regression tests for unattended local schema upgrades."""

from __future__ import annotations

import shutil
import json
import struct
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.migrations import migrate
from app.db.schema_upgrade import ensure_local_databases
from app.db.schema_upgrade import upgrade_shadow


class LocalSchemaUpgradeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                        self.root / "schema")
        (self.root / "data").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _version(self, path: Path) -> tuple[int, int]:
        value = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        return divmod(int(value), 10_000)

    def _latest(self, database: str) -> tuple[int, int]:
        versions = []
        for path in (self.root / "schema" / database / "v1").glob("*.sql"):
            major, minor = path.name.split("_", 1)[0].split("-")
            versions.append((int(major), int(minor)))
        return max(versions)

    def test_empty_pair_uses_current_baselines(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        self.assertEqual(self._version(proxy), self._latest("token-board"))
        self.assertEqual(self._version(dashboard), self._latest("dashboard"))

    def test_v0_pair_is_upgraded_without_manual_transition(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        result = ensure_local_databases(
            str(proxy), str(dashboard), self.root / "schema")
        self.assertTrue(result["token-board"].upgraded)
        self.assertEqual(self._version(proxy), self._latest("token-board"))
        self.assertEqual(self._version(dashboard), self._latest("dashboard"))
        self.assertTrue(list((self.root / "data").glob("auto-v0-to-v1-*.manifest.json")))
        with sqlite3.connect(proxy) as proxy_conn, sqlite3.connect(dashboard) as dash_conn:
            proxy_marker = proxy_conn.execute(
                "SELECT generation_id FROM schema_transitions WHERE transition_id='0-to-1'"
            ).fetchone()
            dash_marker = dash_conn.execute(
                "SELECT generation_id FROM schema_transitions WHERE transition_id='0-to-1'"
            ).fetchone()
        self.assertIsNotNone(proxy_marker)
        self.assertEqual(proxy_marker, dash_marker)

    def test_v0_pair_flattens_legacy_model_pricing(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO model_pricing"
                "(id,model_pattern,input_price,output_price,cache_read_price,currency) "
                "VALUES(1,'legacy-model',1,2,0.5,'CNY')"
            )
            conn.execute(
                "INSERT INTO pricing_slots"
                "(pricing_id,start_minute,end_minute,multiplier) VALUES(1,0,1440,2)"
            )
            conn.commit()
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        with sqlite3.connect(proxy) as conn:
            self.assertEqual(conn.execute(
                "SELECT id,model_pattern,priority,input_price,output_price "
                "FROM pricing_rules"
            ).fetchall(), [(1, "legacy-model", 0, 1.0, 2.0)])
            self.assertEqual(conn.execute(
                "SELECT pricing_rule_id,multiplier FROM pricing_slots"
            ).fetchall(), [(1, 2.0)])
            self.assertFalse(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pricing_rates'"
            ).fetchone())

    def test_v0_pair_imports_durable_usage_spool(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        record = {
            "v": 1,
            "event_id": "local-spool-event",
            "account_id": 0,
            "local_key_id": 0,
            "upstream_key_id": 0,
            "model": "spool-model",
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "cache_read_tokens": 0,
            "total_tokens": 5,
            "cost": 1.25,
            "status_code": 200,
            "requested_at_unix": 1_700_000_000,
            "attempts": [],
        }
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        checksum = 2166136261
        for byte in payload:
            checksum = ((checksum ^ byte) * 16777619) & 0xffffffff
        (Path(str(proxy) + ".request-log.spool")).write_bytes(
            struct.pack("<II", len(payload), checksum) + payload)
        result = ensure_local_databases(
            str(proxy), str(dashboard), self.root / "schema")
        self.assertTrue(result["token-board"].upgraded)
        row = sqlite3.connect(proxy).execute(
            "SELECT event_id,total_tokens,equivalent_cost,pricing_status "
            "FROM request_log WHERE event_id=?", (record["event_id"],)).fetchone()
        self.assertEqual(row, ("local-spool-event", 5, 1.25, "frozen"))
        self.assertFalse(Path(str(proxy) + ".request-log.spool").exists())

    def test_v0_same_mask_historical_credentials_keep_distinct_uuids(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        with sqlite3.connect(proxy) as conn:
            account_id = conn.execute(
                "INSERT INTO upstream_accounts(name,base_url,account_type) "
                "VALUES('rotated','https://old.example','api') RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO upstream_keys(account_id,key_value,position,deleted_at) "
                "VALUES(?,?,0,NULL)",
                (account_id, "sk-same123456tail"),
            )
            conn.execute(
                "INSERT INTO upstream_keys(account_id,key_value,position,deleted_at) "
                "VALUES(?,?,1,'2025-01-01 00:00:00')",
                (account_id, "sk-same999999tail"),
            )
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        with sqlite3.connect(proxy) as conn:
            rows = conn.execute(
                "SELECT c.uuid,c.key_masked FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=? "
                "ORDER BY c.runtime_id", (account_id,)
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], rows[1][1])
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_downloaded_v0_proxy_and_dashboard_artifacts_upgrade_in_shadow(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        # A remote dashboard artifact is resolved against the already-current
        # local proxy identity; its source file is replaced only in the shadow.
        local_proxy = self.root / "data/local-v1-token-board.db"
        migrate(str(local_proxy), str(self.root / "schema"), "token-board")
        remote_proxy = self.root / "data/remote-token-board.db"
        shutil.copy2(proxy, remote_proxy)
        remote_dash = self.root / "data/remote-dashboard.db"
        shutil.copy2(dashboard, remote_dash)
        proxy_result = upgrade_shadow(
            str(remote_proxy), "token-board", self.root / "schema")
        dashboard_result = upgrade_shadow(
            str(remote_dash), "dashboard", self.root / "schema",
            local_token_board_path=str(local_proxy))
        self.assertEqual(proxy_result.current.major, 1)
        self.assertEqual(dashboard_result.current.major, 1)
        self.assertEqual(self._version(remote_proxy), self._latest("token-board"))
        self.assertEqual(self._version(remote_dash), self._latest("dashboard"))
        self.assertEqual(self._version(proxy), (0, 19))

    def test_mixed_local_versions_use_version_routes(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        migrate(str(proxy), str(self.root / "schema/token-board/v0"), "token-board")
        migrate(str(dashboard), str(self.root / "schema"), "dashboard")
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        self.assertEqual(self._version(proxy), self._latest("token-board"))
        self.assertEqual(self._version(dashboard), self._latest("dashboard"))

        proxy = self.root / "data/token-board-mixed.db"
        dashboard = self.root / "data/dashboard-mixed.db"
        migrate(str(proxy), str(self.root / "schema"), "token-board")
        migrate(str(dashboard), str(self.root / "schema/dashboard/v0"), "dashboard")
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        self.assertEqual(self._version(proxy), self._latest("token-board"))
        self.assertEqual(self._version(dashboard), self._latest("dashboard"))


if __name__ == "__main__":
    unittest.main()
