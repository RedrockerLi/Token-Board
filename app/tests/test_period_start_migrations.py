from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db.migrations import SchemaVersion, apply_sql_migrations


class PeriodStartMigrationTest(unittest.TestCase):
    def _schema_root(self, temp: Path) -> Path:
        root = temp / "schema"
        import shutil
        shutil.copytree(Path(__file__).resolve().parents[2] / "schema", root)
        return root

    def test_token_migration_reanchors_open_charge_and_freezes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root = self._schema_root(temp)
            path = temp / "token-board.db"
            apply_sql_migrations(str(path), str(root), "token-board",
                                 target=SchemaVersion(1, 11))
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO accounts(id,uuid,name,valid_from) "
                    "VALUES(1,'account-1','open-plan','2026-07-15')")
                conn.execute(
                    "INSERT INTO billing_contracts"
                    "(id,uuid,account_id,charge_type,billing_scope,currency,"
                    "billing_anchor_day,cooldown_policy_json,valid_from) "
                    "VALUES(1,'contract-1',1,'recurring','account','CNY',15,'{}',"
                    "'2026-07-15T00:00:00Z')")
                conn.execute(
                    "INSERT INTO billing_rate_events"
                    "(contract_id,recurring_price,effective_at,effective_rule) "
                    "VALUES(1,10,'2026-07-15T00:00:00Z','immediate')")
                conn.execute(
                    "INSERT INTO billing_period_charges"
                    "(contract_id,period_start,period_end,recurring_charge,currency,"
                    "normalized_recurring_cost,base_currency) VALUES(1,?,?,?,?,?,?)",
                    ("2026-07-15T00:00:00Z", "2026-08-15T00:00:00Z",
                     20, "CNY", 20, "CNY"),
                )
                conn.commit()
            apply_sql_migrations(str(path), str(root), "token-board")
            with sqlite3.connect(path) as conn:
                charge = conn.execute(
                    "SELECT recurring_charge,normalized_recurring_cost,"
                    "finalized_at FROM billing_period_charges"
                ).fetchone()
            self.assertEqual(charge[:2], (10.0, 10.0))
            self.assertIsNotNone(charge[2])

    def test_dashboard_migration_removes_pure_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root = self._schema_root(temp)
            path = temp / "dashboard.db"
            apply_sql_migrations(str(path), str(root), "dashboard",
                                 target=SchemaVersion(1, 4))
            with sqlite3.connect(path) as conn:
                conn.executemany(
                    "INSERT INTO accounts(account_id,name,account_kind) "
                    "VALUES(?,?,?)",
                    [(1, "zero-only", "proxy"), (2, "has-charge", "proxy")],
                )
                conn.execute(
                    "INSERT INTO monthly_recurring_costs"
                    "(month,account_id,billing_unit_id,recurring_charge,"
                    "equivalent_cost,normalized_recurring_cost) "
                    "VALUES('2026-08',1,'unit-zero',0,0,0)")
                conn.execute(
                    "INSERT INTO monthly_recurring_costs"
                    "(month,account_id,billing_unit_id,recurring_charge,"
                    "equivalent_cost,normalized_recurring_cost) "
                    "VALUES('2026-08',2,'unit-charge',10,0,10)")
                conn.commit()
            apply_sql_migrations(str(path), str(root), "dashboard")
            with sqlite3.connect(path) as conn:
                zero = conn.execute(
                    "SELECT count(*) FROM monthly_recurring_costs "
                    "WHERE account_id=1").fetchone()[0]
                accounts = conn.execute(
                    "SELECT account_id FROM accounts ORDER BY account_id"
                ).fetchall()
                frozen = conn.execute(
                    "SELECT charge_frozen_at FROM monthly_recurring_costs "
                    "WHERE account_id=2").fetchone()[0]
            self.assertEqual(zero, 0)
            self.assertEqual(accounts, [(2,)])
            self.assertIsNone(frozen)


if __name__ == "__main__":
    unittest.main()
