from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.db.dashboard_db import DashboardDatabase
from app.db.migrations import SchemaVersion, apply_sql_migrations
from app.db.proxy.billing import materialize_period_charges
from app.db.schema_upgrade import ensure_local_databases
from app.services.billing_report import actual_cost

from app.tests.support import AppDatabaseTestCase


class V2MigrationTest(unittest.TestCase):
    def test_v1_pair_is_upgraded_without_soft_delete_or_receipt_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                            root / "schema")
            (root / "data").mkdir()
            proxy = root / "data/token-board.db"
            dashboard = root / "data/dashboard.db"
            apply_sql_migrations(str(proxy), str(root / "schema"),
                                 "token-board", SchemaVersion(1, 21))
            apply_sql_migrations(str(dashboard), str(root / "schema"),
                                 "dashboard", SchemaVersion(1, 7))

            ensure_local_databases(str(proxy), str(dashboard), root / "schema")
            second = ensure_local_databases(str(proxy), str(dashboard), root / "schema")
            self.assertFalse(second["token-board"].upgraded)
            self.assertFalse(second["dashboard"].upgraded)

            with sqlite3.connect(proxy) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 20_000)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
                self.assertNotIn("lifecycle_state", columns)
                self.assertNotIn("deleted_at", columns)
                self.assertNotIn("disabled_at", columns)
                self.assertNotIn("valid_until", {
                    row[1] for row in conn.execute("PRAGMA table_info(billing_contracts)")
                })
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_terminal_v1_graph_is_deleted_while_history_is_detached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                            root / "schema")
            (root / "data").mkdir()
            proxy = root / "data/token-board.db"
            dashboard = root / "data/dashboard.db"
            apply_sql_migrations(str(proxy), str(root / "schema"),
                                 "token-board", SchemaVersion(1, 21))
            apply_sql_migrations(str(dashboard), str(root / "schema"),
                                 "dashboard", SchemaVersion(1, 7))
            with sqlite3.connect(proxy) as conn:
                conn.executescript(
                    """
                    INSERT INTO accounts
                        (id,uuid,name,lifecycle_state,valid_from,deleted_at,
                         account_kind,created_at,updated_at)
                    VALUES
                        (42,'v1-proxy-42','terminal','deleted','2026-01-01',
                         '2026-01-02T00:00:00Z','proxy',
                         '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z');
                    INSERT INTO upstreams(id,account_id,name,base_url)
                    VALUES(42,42,'terminal','https://example.test');
                    INSERT INTO route_sets(id,uuid,account_id,name)
                    VALUES(42,'v1-route-42',42,'terminal');
                    INSERT INTO route_rules(route_set_id,model_pattern,upstream_id)
                    VALUES(42,'*',42);
                    INSERT INTO client_keys(id,uuid,key_value,route_set_id)
                    VALUES(42,'v1-key-42','local',42);
                    INSERT INTO upstream_credentials
                        (uuid,runtime_id,upstream_id,key_masked,deleted_at)
                    VALUES('v1-credential-42',42,42,'sk-old',
                           '2026-01-02T00:00:00Z');
                    INSERT INTO upstream_secrets(credential_uuid,secret_value)
                    VALUES('v1-credential-42','sk-old');
                    INSERT INTO billing_contracts
                        (id,uuid,account_id,charge_type,billing_scope,valid_from)
                    VALUES(42,'v1-contract-42',42,'recurring','credential',
                           '2026-01-01T00:00:00Z');
                    INSERT INTO billing_period_charges
                        (contract_id,credential_uuid,period_start,period_end,
                         recurring_charge,currency,normalized_recurring_cost,
                         finalized_at,account_identity_id,contract_uuid_snapshot,
                         billing_unit_id)
                    VALUES(42,'v1-credential-42','2026-01-01T00:00:00Z',
                           '2026-02-01T00:00:00Z',39,'CNY',39,
                           '2026-01-01T00:00:00Z',42,'v1-contract-42',
                           'v1-credential-42');
                    INSERT INTO request_log
                        (event_id,source_kind,account_id,route_set_id,client_key_id,
                         credential_uuid,model,status_code,requested_at,
                         account_identity_id,billing_unit_id)
                    VALUES('v1-terminal-request','import',42,42,42,
                           'v1-credential-42','model',200,
                           '2026-01-01T00:00:00Z',42,'v1-credential-42');
                    """
                )
                conn.commit()

            ensure_local_databases(str(proxy), str(dashboard), root / "schema")
            with sqlite3.connect(proxy) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM accounts WHERE id=42").fetchone())
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM upstream_credentials WHERE runtime_id=42"
                ).fetchone())
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM account_identities WHERE id=42"
                ).fetchone())
                self.assertEqual(conn.execute(
                    "SELECT account_id,route_set_id,client_key_id,credential_uuid "
                    "FROM request_log WHERE event_id='v1-terminal-request'"
                ).fetchone(), (None, None, None, None))
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM billing_period_charges "
                    "WHERE account_identity_id=42"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            with sqlite3.connect(dashboard) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 20_000)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='billing_export_receipts'"
                ).fetchone())
                self.assertIn("period_start", {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(monthly_recurring_costs)")
                })
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])


class V2HardDeleteBillingTest(AppDatabaseTestCase):
    def test_api_delete_physically_removes_live_graph_but_keeps_identity(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "v2-api", "account_type": "api",
            "base_url": "https://example.test", "upstream_keys": ["secret"],
        })
        result = db.delete_account(account_id, mode="immediate")

        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["effective_ends_at"])
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_credentials c JOIN upstreams u "
                "ON u.id=c.upstream_id WHERE u.account_id=?", (account_id,)
            ).fetchone()[0], 0)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM account_identities WHERE id=?", (account_id,)
            ).fetchone())
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_plan_is_billed_per_key_once_then_delete_preserves_frozen_facts(self) -> None:
        db = self.proxy_database()
        db.update_plan_billing_config({"cancellation_mode": "immediate"})
        account_id = db.create_account({
            "name": "v2-plan", "account_type": "plan", "monthly_price": 39,
            "currency": "CNY", "base_url": "https://example.test",
            "upstream_keys": ["first", "second"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM billing_period_charges "
                "WHERE account_identity_id=? AND finalized_at IS NOT NULL",
                (account_id,),
            ).fetchone()[0], 2)
            conn.execute(
                "UPDATE upstream_credentials SET enabled=0 WHERE upstream_id IN "
                "(SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
            conn.commit()

        materialize_period_charges(self.proxy_path)
        result = db.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)

        with sqlite3.connect(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM billing_period_charges "
                "WHERE account_identity_id=? AND finalized_at IS NOT NULL",
                (account_id,),
            ).fetchone()[0], 2)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM billing_export_events "
                "WHERE account_id=?", (account_id,)
            ).fetchone()[0], 2)

    def test_actual_cost_is_metered_plus_recurring_and_dashboard_rows_are_physical(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "v2-report", "account_type": "plan", "monthly_price": 10,
            "currency": "CNY", "base_url": "https://example.test",
            "upstream_keys": ["report-key"],
        })
        now = datetime.now(timezone.utc).replace(microsecond=0)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "account_identity_id,model,status_code,requested_at,total_tokens,"
                "equivalent_cost,billed_usage_cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("v2-report-event", "proxy", account_id, account_id, "model", 200,
                 timestamp, 1, 7, 7),
            )
            conn.commit()

        with sqlite3.connect(self.proxy_path) as conn:
            costs = actual_cost(conn, now=now)
        self.assertEqual(costs["metered_cost"], 7)
        self.assertEqual(costs["total_cost"],
                         costs["metered_cost"] + costs["recurring_cost"])

        dashboard = DashboardDatabase(str(self.dashboard_path), schema_dir=str(self.root / "schema"))
        dashboard.upsert_account_batch([{
            "account_id": account_id, "name": "v2-report",
            "updated_at": timestamp, "account_kind": "proxy",
        }])
        dashboard.upsert_proxy_data(
            now.date().isoformat(), "model", account_id, 1, 0, 0, 1, 7, 7)
        dashboard.upsert_frozen_plan_charge(
            period_start=timestamp[:10] + "T00:00:00Z", account_id=account_id,
            billing_unit_id="report-key", recurring_charge=10,
            normalized_recurring_cost=10, currency="CNY", base_currency="CNY",
            fx_rate_date=None, frozen_at=timestamp)
        self.assertGreaterEqual(dashboard.purge_accounts({account_id}), 3)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=?", (account_id,)
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM monthly_recurring_costs WHERE account_id=?",
                (account_id,),
            ).fetchone()[0], 0)

    def test_actual_cost_excludes_frozen_charges_after_live_account_delete(self) -> None:
        db = self.proxy_database()
        db.update_plan_billing_config({"cancellation_mode": "immediate"})
        live_id = db.create_account({
            "name": "live-plan", "account_type": "plan", "monthly_price": 10,
            "currency": "CNY", "base_url": "https://live.example",
            "upstream_keys": ["live-key"],
        })
        deleted_id = db.create_account({
            "name": "deleted-plan", "account_type": "plan", "monthly_price": 99,
            "currency": "CNY", "base_url": "https://deleted.example",
            "upstream_keys": ["deleted-key"],
        })
        now = datetime.now(timezone.utc).replace(microsecond=0)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with sqlite3.connect(self.proxy_path) as conn:
            conn.executemany(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "account_identity_id,model,status_code,requested_at,total_tokens,"
                "equivalent_cost,billed_usage_cost) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    ("live-current-request", "proxy", live_id, live_id,
                     "model", 200, timestamp, 1, 2, 2),
                    ("deleted-old-request", "proxy", deleted_id, deleted_id,
                     "model", 200, timestamp, 1, 99, 99),
                ],
            )
            conn.commit()

        # This keeps the old finalized charge and identity for audit while
        # removing the live configuration graph.
        self.assertTrue(db.delete_account(deleted_id, mode="immediate")["ok"])
        with sqlite3.connect(self.proxy_path) as conn:
            costs = actual_cost(conn, now=now)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM billing_period_charges "
                "WHERE account_identity_id=? AND finalized_at IS NOT NULL",
                (deleted_id,),
            ).fetchone()[0], 1)

        self.assertAlmostEqual(costs["metered_cost"], 2.0)
        self.assertAlmostEqual(costs["recurring_cost"], 10.0)
        self.assertAlmostEqual(costs["total_cost"], 12.0)


if __name__ == "__main__":
    unittest.main()
