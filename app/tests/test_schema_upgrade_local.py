"""Regression tests for unattended local schema upgrades."""

from __future__ import annotations

import shutil
import json
import struct
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.migrations import SchemaVersion, apply_sql_migrations, migrate
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
        major_dirs = list((self.root / "schema" / database).glob("v[0-9]*"))
        latest_major_dir = max(major_dirs, key=lambda path: int(path.name[1:]))
        for path in latest_major_dir.glob("*.sql"):
            major, minor = path.name.split("_", 1)[0].split("-")
            versions.append((int(major), int(minor)))
        return max(versions)

    def test_empty_pair_uses_current_baselines(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        self.assertEqual(self._version(proxy), self._latest("token-board"))
        self.assertEqual(self._version(dashboard), self._latest("dashboard"))

    def test_current_proxy_schema_treats_every_business_name_as_display_data(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        ensure_local_databases(str(proxy), str(dashboard), self.root / "schema")
        for database in (proxy, dashboard):
            with sqlite3.connect(database) as conn:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )]
                for table in tables:
                    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    name_columns = [row for row in columns if row[1] == "name"]
                    if not name_columns:
                        continue
                    self.assertEqual(name_columns[0][5], 0, f"{database}: {table}")
                    for index in conn.execute(f"PRAGMA index_list({table})"):
                        if not index[2]:
                            continue
                        indexed_columns = [row[2] for row in conn.execute(
                            f"PRAGMA index_info({index[1]})")]
                        self.assertNotEqual(indexed_columns, ["name"],
                                            f"{database}: {table}: {index[1]}")

    def test_agent_subscription_identity_migration_preserves_frozen_charge(self) -> None:
        proxy = self.root / "data/token-board.db"
        apply_sql_migrations(
            str(proxy), str(self.root / "schema"), "token-board",
            target=SchemaVersion(1, 18),
        )
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(1,'agent-account','date-agent','agent')"
            )
            conn.execute(
                "INSERT INTO agent_software"
                "(id,uuid,name,agent_kind) VALUES(1,'software-date','date-agent','codex')"
            )
            conn.execute(
                "INSERT INTO agent_subscriptions"
                "(id,uuid,name,currency,valid_from,created_at,updated_at) "
                "VALUES(7,'subscription-7','same-name','CNY','2026-07-01',"
                "'2026-07-01T00:00:00Z','2026-07-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO agent_subscription_instances"
                "(id,uuid,subscription_id,label,valid_from,created_at,updated_at) "
                "VALUES(8,'instance-8',7,'default','2026-07-01',"
                "'2026-07-01T00:00:00Z','2026-07-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO agent_subscription_period_charges"
                "(id,instance_id,subscription_id,period_start,period_end,"
                "recurring_charge,currency,normalized_recurring_cost,"
                "subscription_uuid_snapshot,instance_uuid_snapshot,"
                "subscription_name_snapshot,instance_label_snapshot,finalized_at) "
                "VALUES(9,8,7,'2026-07-01T00:00:00Z','2026-08-01T00:00:00Z',"
                "10,'CNY',10,'subscription-7','instance-8','same-name','default',"
                "'2026-07-01T00:00:01Z')"
            )
            conn.commit()

        apply_sql_migrations(str(proxy), str(self.root / "schema"), "token-board")

        with sqlite3.connect(proxy) as conn:
            self.assertEqual(conn.execute(
                "SELECT uuid,name,currency FROM agent_subscription_identities "
                "WHERE id=7").fetchone(),
                ("subscription-7", "same-name", "CNY"))
            self.assertEqual(conn.execute(
                "SELECT uuid,subscription_id,label FROM "
                "agent_subscription_instance_identities WHERE id=8"
            ).fetchone(), ("instance-8", 7, "default"))
            self.assertEqual(conn.execute(
                "SELECT subscription_id,instance_id,recurring_charge "
                "FROM agent_subscription_period_charges WHERE id=9"
            ).fetchone(), (7, 8, 10.0))
            self.assertEqual(conn.execute(
                "PRAGMA foreign_key_list(agent_subscription_period_charges)"
            ).fetchall(), [])
            conn.execute(
                "INSERT INTO agent_subscriptions"
                "(uuid,name,currency,valid_from,created_at,updated_at) "
                "VALUES('subscription-9','same-name','CNY','2026-07-01',"
                "'2026-07-01T00:00:00Z','2026-07-01T00:00:00Z')"
            )
            conn.rollback()

    def test_v120_removes_name_unique_indexes_without_losing_live_rows(self) -> None:
        proxy = self.root / "data/token-board.db"
        apply_sql_migrations(
            str(proxy), str(self.root / "schema"), "token-board",
            target=SchemaVersion(1, 19),
        )
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(7,'account-7','same-display-name','proxy')"
            )
            conn.execute(
                "INSERT INTO route_sets(id,uuid,account_id,name) "
                "VALUES(8,'route-8',7,'same-display-name')"
            )
            conn.execute(
                "INSERT INTO agent_software"
                "(id,uuid,name,agent_kind) VALUES(9,'software-9',"
                "'same-display-name','codex')"
            )
            conn.commit()

        apply_sql_migrations(str(proxy), str(self.root / "schema"), "token-board")

        with sqlite3.connect(proxy) as conn:
            for table in ("accounts", "route_sets", "agent_software"):
                unique_name_indexes = []
                for index in conn.execute(f"PRAGMA index_list({table})"):
                    if not index[2]:
                        continue
                    columns = [row[2] for row in conn.execute(
                        f"PRAGMA index_info({index[1]})")]
                    if columns == ["name"]:
                        unique_name_indexes.append(index[1])
                self.assertEqual(unique_name_indexes, [], table)
            self.assertEqual(conn.execute(
                "SELECT name FROM accounts WHERE id=7").fetchone()[0],
                "same-display-name")
            self.assertEqual(conn.execute(
                "SELECT name FROM route_sets WHERE id=8").fetchone()[0],
                "same-display-name")
            self.assertEqual(conn.execute(
                "SELECT name FROM agent_software WHERE id=9").fetchone()[0],
                "same-display-name")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v121_normalizes_subscription_effective_values_to_utc_dates(self) -> None:
        proxy = self.root / "data/token-board.db"
        apply_sql_migrations(
            str(proxy), str(self.root / "schema"), "token-board",
            target=SchemaVersion(1, 20),
        )
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO agent_subscriptions"
                "(id,uuid,name,currency,valid_from,created_at,updated_at) "
                "VALUES(7,'subscription-date','date-sub','CNY',"
                "'2026-09-06T06:56:13Z','2026-09-06T06:56:13Z',"
                "'2026-09-06T06:56:13Z')"
            )
            conn.execute(
                "INSERT INTO agent_subscription_instances"
                "(id,uuid,subscription_id,label,valid_from,created_at,updated_at) "
                "VALUES(8,'instance-date',7,'default','2026-09-06T06:56:13Z',"
                "'2026-09-06T06:56:13Z','2026-09-06T06:56:13Z')"
            )
            conn.execute(
                "INSERT INTO agent_subscription_bindings"
                "(id,subscription_id,software_id,valid_from,updated_at) "
                "VALUES(9,7,1,'2026-09-06T06:56:13Z','2026-09-06T06:56:13Z')"
            )
            conn.execute(
                "INSERT INTO billing_contracts"
                "(id,uuid,account_id,charge_type,billing_scope,valid_from) "
                "VALUES(7,'contract-date',1,'recurring','account',"
                "'2026-09-06T06:56:13Z')"
            )
            conn.commit()

        apply_sql_migrations(str(proxy), str(self.root / "schema"), "token-board")

        with sqlite3.connect(proxy) as conn:
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM agent_subscriptions WHERE id=7"
            ).fetchone()[0], "2026-09-06")
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM agent_subscription_instances WHERE id=8"
            ).fetchone()[0], "2026-09-06")
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM agent_subscription_bindings WHERE id=9"
            ).fetchone()[0], "2026-09-06")
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM agent_subscription_instance_identities "
                "WHERE id=8"
            ).fetchone()[0], "2026-09-06")
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM billing_contracts WHERE id=7"
            ).fetchone()[0], "2026-09-06")

    def test_dashboard_v15_upgrade_removes_account_exclusions(self) -> None:
        dashboard = self.root / "data/dashboard.db"
        apply_sql_migrations(
            str(dashboard), str(self.root / "schema"), "dashboard",
            target=SchemaVersion(1, 5),
        )
        with sqlite3.connect(dashboard) as conn:
            conn.execute(
                "INSERT INTO account_exclusions(account_id) VALUES(24)")
            conn.commit()

        apply_sql_migrations(str(dashboard), str(self.root / "schema"), "dashboard")

        with sqlite3.connect(dashboard) as conn:
            self.assertEqual(self._version(dashboard), (1, 7))
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='account_exclusions'"
            ).fetchone())

    def test_v17_frozen_charge_becomes_delivered_event_baseline(self) -> None:
        proxy = self.root / "data/token-board.db"
        dashboard = self.root / "data/dashboard.db"
        apply_sql_migrations(
            str(proxy), str(self.root / "schema"), "token-board",
            target=SchemaVersion(1, 17),
        )
        apply_sql_migrations(
            str(dashboard), str(self.root / "schema"), "dashboard",
            target=SchemaVersion(1, 6),
        )
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(7,'account-7','legacy-plan','proxy')"
            )
            conn.execute(
                "INSERT INTO billing_contracts"
                "(id,uuid,account_id,charge_type,billing_scope,currency,"
                "billing_anchor_day,valid_from) "
                "VALUES(3,'contract-3',7,'recurring','account','CNY',1,?)",
                ("2026-01-01T00:00:00Z",),
            )
            conn.execute(
                "INSERT INTO billing_period_charges"
                "(id,contract_id,period_start,period_end,recurring_charge,"
                "currency,normalized_recurring_cost,base_currency,finalized_at,"
                "account_identity_id,contract_uuid_snapshot,billing_unit_id) "
                "VALUES(9,3,'2026-08-01T00:00:00Z','2026-09-01T00:00:00Z',"
                "12,'CNY',12,'CNY','2026-08-01T00:00:01Z',7,'contract-3',"
                "'contract:contract-3')"
            )
            conn.commit()
        with sqlite3.connect(dashboard) as conn:
            conn.execute(
                "INSERT INTO accounts(account_id,name,account_kind) "
                "VALUES(7,'legacy-plan','proxy')"
            )
            conn.execute(
                "INSERT INTO monthly_recurring_costs"
                "(month,account_id,billing_unit_id,recurring_charge,"
                "normalized_recurring_cost,charge_frozen_at) "
                "VALUES('2026-08',7,'contract:contract-3',12,12,"
                "'2026-08-01T00:00:01Z')"
            )
            conn.commit()

        apply_sql_migrations(str(proxy), str(self.root / "schema"), "token-board")
        apply_sql_migrations(str(dashboard), str(self.root / "schema"), "dashboard")

        with sqlite3.connect(proxy) as conn:
            self.assertEqual(conn.execute(
                "SELECT event_key,event_kind,source_key FROM billing_export_events"
            ).fetchall(), [
                ("proxy:contract:contract-3:2026-08-01T00:00:00Z",
                 "proxy", "9")
            ])
            self.assertEqual(conn.execute(
                "SELECT value FROM sync_state "
                "WHERE key='last_exported_billing_event_id'"
            ).fetchone()[0], "1")
        with sqlite3.connect(dashboard) as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='billing_export_receipts'"
            ).fetchone())
            self.assertEqual(conn.execute(
                "SELECT event_key,billing_unit_id,payload_hash "
                "FROM billing_export_receipts"
            ).fetchone(),
                ("legacy:7:2026-08:contract:contract-3",
                 "contract:contract-3", ""))

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
        # The deleted V0 key is terminal live configuration and is physically
        # removed during V2 conversion; only the active key remains.
        self.assertEqual(len(rows), 1)

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
        self.assertEqual(proxy_result.current.major, 2)
        self.assertEqual(dashboard_result.current.major, 2)
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
