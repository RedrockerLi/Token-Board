"""03-F-16/19/20/23: app/db dead-code deletions, asserted behaviourally.

The audit items are deletions, so these cases assert the *observable* result
(archive merge rows, export counts and dashboard totals) plus the absence of
the deleted callables through ``hasattr``.  Source-literal greps are
deliberately absent: the implementation removed the literals from comments too,
so a grep would neither prove nor protect the behaviour.

Dead-code list: ref/audit/03-dead-code-orphans.md §5.4, §6.1, §6.2, §6.5.
"""

from __future__ import annotations

import ast
import unittest

from app.db.dashboard_db import DashboardDatabase
from app.db.proxy.billing_ledger import ProxyBillingLedgerMixin
from app.tests.support import AppDatabaseTestCase, PROJECT_ROOT, sqlite_connection

EXPORT_PATH = PROJECT_ROOT / "app/db/proxy/export.py"


class DashboardArchiveBehaviorTest(AppDatabaseTestCase):
    """03-F-16: the archive merge result is unchanged by deleting ``id<>0``."""

    def _dashboard(self) -> DashboardDatabase:
        return DashboardDatabase(str(self.dashboard_path),
                                 schema_dir=self.root / "schema")

    def test_archive_users_contract_is_unchanged(self) -> None:
        dashboard = self._dashboard()
        with sqlite_connection(self.dashboard_path) as conn:
            # A fresh dashboard already owns the id=0 merge target.
            conn.execute(
                "INSERT OR IGNORE INTO users(id,name,actual_cost_micro_cny) "
                "VALUES(0,'merged',0)")
            conn.execute(
                "INSERT INTO users(id,name,actual_cost_micro_cny) "
                "VALUES(7,'alice',0)")
            conn.executescript("""
                INSERT INTO daily_model_usage(
                    user_id,usage_date,model,input_tokens,cache_read_tokens,
                    output_tokens,request_count,api_equivalent_cost_micro_cny)
                VALUES(7,'2026-08-05','m-a',100,0,10,1,5000),
                      (0,'2026-08-05','m-a',50,0,5,1,2500);
            """)

        self.assertEqual(dashboard.archive_users([]), 0)
        # 0 is the merge target and cannot itself be archived.
        with self.assertRaises(ValueError) as zero:
            dashboard.archive_users([0])
        self.assertIn("archive user_id=0 cannot be archived", str(zero.exception))
        with self.assertRaises(ValueError):
            dashboard.archive_users([-1])
        # Unknown ids are skipped; a known id merges its daily rows into 0.
        self.assertEqual(dashboard.archive_users([999]), 0)
        self.assertEqual(dashboard.archive_users([7]), 1)

        with sqlite_connection(self.dashboard_path) as conn:
            merged = [list(row) for row in conn.execute(
                "SELECT user_id,input_tokens,request_count,"
                "api_equivalent_cost_micro_cny FROM daily_model_usage "
                "ORDER BY user_id")]
            source_rows = conn.execute(
                "SELECT count(*) FROM users WHERE id=7").fetchone()[0]
        self.assertEqual(merged, [[0, 150, 2, 7500]])
        self.assertEqual(source_rows, 0)


class ExportBehaviorTest(AppDatabaseTestCase):
    """03-F-19/20/23: the export output is unchanged by deleting the C block."""

    def test_export_counts_and_dashboard_totals_are_unchanged(self) -> None:
        with sqlite_connection(self.proxy_path) as conn:
            conn.executescript("""
                INSERT INTO accounts(id,uuid,name,account_kind,valid_from)
                     VALUES(1,'a1','acct-1','proxy','2026-01-01'),
                           (2,'a2','acct-2','proxy','2026-01-01');
                INSERT INTO billing_contracts(
                    id,uuid,account_id,charge_type,billing_scope,valid_from)
                     VALUES(1,'c1',1,'recurring','account',
                            '2026-01-01T00:00:00Z');
                INSERT INTO request_log(
                    event_id,source_kind,account_id,model,prompt_tokens,
                    completion_tokens,cache_read_tokens,total_tokens,
                    equivalent_cost,billed_usage_cost,status_code,
                    requested_at,pricing_status)
                     VALUES('e1','proxy',1,'m-a',100,10,0,110,0.5,0.5,200,
                            '2026-08-05T10:00:00Z','rated'),
                           ('e2','proxy',2,'m-b',200,20,0,220,0.7,0.0,200,
                            '2026-08-05T11:00:00Z','rated'),
                           ('e3','proxy',1,'m-a',300,30,0,330,0.9,0.9,200,
                            '2026-08-06T10:00:00Z','rated');
            """)
            max_log = conn.execute(
                "SELECT max(id) FROM request_log").fetchone()[0]

        # The recurring contract makes the export materialize one frozen
        # charge for its current period, which becomes the single billing
        # export event counted below (the C block never contributed to it).
        target = self.root / "data/exported-dashboard.db"
        from app.db.schema_upgrade import ensure_local_databases
        ensure_local_databases(str(self.proxy_path), str(target),
                               self.root / "schema")
        result = self.proxy_database().export_to_dashboard(
            str(target), 0, max_log)

        self.assertEqual(result, {
            "record_count": 3,
            "dashboard_records": 3,
            "billing_event_count": 1,
            "billing_max_id": 1,
        })
        with sqlite_connection(target) as conn:
            rows = [list(row) for row in conn.execute(
                "SELECT user_id,usage_date,model,request_count "
                "FROM daily_model_usage ORDER BY user_id,usage_date,model")]
            totals = tuple(conn.execute(
                "SELECT count(*),COALESCE(SUM(request_count),0),"
                "COALESCE(SUM(api_equivalent_cost_micro_cny),0) "
                "FROM daily_model_usage").fetchone())
        self.assertEqual(rows, [
            [1, "2026-08-05", "m-a", 1],
            [1, "2026-08-06", "m-a", 1],
            [2, "2026-08-05", "m-b", 1],
        ])
        self.assertEqual(totals, (3, 3, 2_100_000))


class DeadCodeRemovedTest(unittest.TestCase):
    def test_dashboard_writer_has_no_stub_plan_summary(self) -> None:
        # 03-F-19: the compatibility no-op is gone, not merely unused.
        self.assertFalse(hasattr(DashboardDatabase, "accumulate_plan_summary"))

    def test_billing_ledger_has_no_dead_plan_meta_helper(self) -> None:
        # 03-F-20 collateral cleanup: the helper's only caller was the C block.
        self.assertFalse(hasattr(ProxyBillingLedgerMixin,
                                 "_plan_key_billing_meta"))

    def test_export_has_no_unused_now_local(self) -> None:
        # 03-F-23: `now = utc_now()...` was assigned and never read.  This is an
        # AST data-flow check rather than a grep: a future reader that actually
        # uses the variable would pass.
        tree = ast.parse(EXPORT_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned = sorted(
                child.lineno for child in ast.walk(node)
                if isinstance(child, ast.Name) and child.id == "now"
                and isinstance(child.ctx, ast.Store))
            if not assigned:
                continue
            read = any(
                isinstance(child, ast.Name) and child.id == "now"
                and isinstance(child.ctx, ast.Load)
                for child in ast.walk(node))
            self.assertTrue(read,
                            f"03-F-23: export.py:{assigned} assigns `now` "
                            "without ever reading it")


if __name__ == "__main__":
    unittest.main()
