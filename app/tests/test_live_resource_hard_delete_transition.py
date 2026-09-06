from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.migrations import SchemaVersion, apply_sql_migrations
from app.db.schema_upgrade import ensure_local_databases


class LiveResourceHardDeleteTransitionTest(unittest.TestCase):
    def test_purges_terminal_proxy_graph_but_keeps_identity_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(Path(__file__).resolve().parents[2] / "schema",
                            root / "schema")
            (root / "data").mkdir()
            proxy = root / "data/token-board.db"
            dashboard = root / "data/dashboard.db"
            # Build a complete V1 pair, then exercise the actual V1 -> V2
            # shadow migration. The runtime itself is V2-only after this
            # point; the legacy columns below exist only in this fixture.
            apply_sql_migrations(str(proxy), str(root / "schema"),
                                 "token-board", target=SchemaVersion(1, 21))
            apply_sql_migrations(str(dashboard), str(root / "schema"),
                                 "dashboard", target=SchemaVersion(1, 7))

            with sqlite3.connect(proxy) as conn:
                conn.executescript(
                    """
                    INSERT INTO accounts
                        (id,uuid,name,lifecycle_state,valid_from,deleted_at,
                         account_kind,created_at,updated_at)
                    VALUES
                        (42,'proxy-42','old-soft-delete','deleted',
                         '2026-01-01','2026-01-02T00:00:00Z','proxy',
                         '2026-01-01T00:00:00Z','2026-01-02T00:00:00Z');
                    INSERT INTO upstreams
                        (id,account_id,name,base_url)
                    VALUES (42,42,'old-soft-delete','https://example.test');
                    INSERT INTO route_sets(id,uuid,account_id,name)
                    VALUES (42,'route-42',42,'old-soft-delete');
                    INSERT INTO client_keys
                        (id,uuid,key_value,route_set_id)
                    VALUES (42,'key-42','tk-old',42);
                    INSERT INTO route_rules
                        (id,route_set_id,model_pattern,upstream_id)
                    VALUES (42,42,'*',42);
                    INSERT INTO upstream_credentials
                        (uuid,runtime_id,upstream_id,key_masked)
                    VALUES ('credential-42',42,42,'sk-old');
                    INSERT INTO upstream_secrets(credential_uuid,secret_value)
                    VALUES ('credential-42','sk-old');
                    INSERT INTO billing_contracts
                        (id,uuid,account_id,charge_type,billing_scope,
                         valid_from)
                    VALUES (42,'contract-42',42,'recurring','account',
                            '2026-01-01T00:00:00Z');
                    INSERT INTO billing_rate_events
                        (id,contract_id,recurring_price,effective_at)
                    VALUES (42,42,10,'2026-01-01T00:00:00Z');
                    INSERT INTO request_log
                        (event_id,source_kind,account_id,route_set_id,
                         client_key_id,credential_uuid,model,status_code,
                         requested_at,account_identity_id,billing_unit_id,
                         billing_contract_uuid,billing_anchor_day)
                    VALUES ('event-42','import',42,42,42,'credential-42',
                            'old-model',200,'2026-01-01T00:00:00Z',42,
                            'contract:contract-42','contract-42',1);
                    INSERT INTO billing_period_charges
                        (id,contract_id,period_start,period_end,
                         recurring_charge,currency,normalized_recurring_cost,
                         finalized_at,account_identity_id,
                         contract_uuid_snapshot,billing_unit_id)
                    VALUES (42,42,'2026-01-01T00:00:00Z',
                            '2026-02-01T00:00:00Z',10,'CNY',10,
                            '2026-01-01T00:00:00Z',42,'contract-42',
                            'contract:contract-42');
                    """
                )
                conn.commit()

            ensure_local_databases(str(proxy), str(dashboard), root / "schema")

            with sqlite3.connect(proxy) as conn:
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM accounts WHERE id=42").fetchone())
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM account_identities WHERE id=42").fetchone())
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM billing_period_charges "
                    "WHERE account_identity_id=42").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT account_id,route_set_id,client_key_id,credential_uuid "
                    "FROM request_log WHERE event_id='event-42'"
                ).fetchone(), (None, None, None, None))
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM upstreams WHERE account_id=42"
                ).fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
