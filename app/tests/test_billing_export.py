from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

from app.db.proxy.billing import materialize_period_charges
from app.db.proxy_db import ProxyDatabase
from app.services.sync.dashboard_sync import sync_dashboard
from app.services.sync.dashboard_user_delete import delete_dashboard_users
from app.services.sync.state import get_sync_state

from app.tests.support import AppDatabaseTestCase


class BillingExportTest(AppDatabaseTestCase):
    def _create_plan(self) -> int:
        database = self.proxy_database()
        account_id = database.create_account({
            "name": "immutable-plan",
            "account_type": "plan",
            "currency": "CNY",
            "monthly_price": 12,
            "valid_from": "2026-07-01",
            "base_url": "http://example.test",
            "upstream_keys": ["sk-immutable"],
            "new_valid_froms": ["2026-07-01"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_rate_events SET effective_at=?",
                ("2026-06-01T00:00:00Z",),
            )
            conn.commit()
        return account_id

    def _dashboard_charge_count(self, account_id: int) -> int:
        with sqlite3.connect(self.dashboard_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM monthly_recurring_costs WHERE account_id=?",
                (account_id,),
            ).fetchone()[0]

    def test_frozen_bill_is_exported_once_and_does_not_return_after_delete(self) -> None:
        account_id = self._create_plan()
        materialize_period_charges(
            self.proxy_path,
            datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        first = sync_dashboard(
            self.proxy_path, self.dashboard_path,
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(first["status"], "ok", first)
        self.assertGreater(self._dashboard_charge_count(account_id), 0)
        first_mark = get_sync_state(
            self.proxy_path, "last_exported_billing_event_id")
        self.assertIsNotNone(first_mark)
        self.assertEqual(get_sync_state(
            self.proxy_path, "last_exported_log_id"), "0")

        deleted = delete_dashboard_users(
            self.proxy_path, self.dashboard_path, ["immutable-plan"],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(deleted["status"], "ok", deleted)
        self.assertEqual(self._dashboard_charge_count(account_id), 0)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertGreater(conn.execute(
                "SELECT count(*) FROM billing_export_receipts "
                "WHERE event_key LIKE 'legacy:%'"
            ).fetchone()[0], 0)

        second = sync_dashboard(
            self.proxy_path, self.dashboard_path,
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(self._dashboard_charge_count(account_id), 0)
        self.assertEqual(
            get_sync_state(self.proxy_path, "last_exported_billing_event_id"),
            first_mark,
        )

    def test_billing_mark_waits_for_local_commit(self) -> None:
        account_id = self._create_plan()
        materialize_period_charges(
            self.proxy_path,
            datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        before_mark = get_sync_state(
            self.proxy_path, "last_exported_billing_event_id")

        original_copy = __import__(
            "app.services.sync.dashboard_sync", fromlist=["safe_copy_db"]
        ).safe_copy_db
        calls = {"count": 0}

        def fail_at_local_commit(source: str, target: str) -> None:
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated dashboard install failure")
            original_copy(source, target)

        with patch("app.services.sync.dashboard_sync.safe_copy_db",
                   side_effect=fail_at_local_commit):
            result = sync_dashboard(
                self.proxy_path, self.dashboard_path,
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "error", result)
        self.assertEqual(
            get_sync_state(self.proxy_path, "last_exported_billing_event_id"),
            before_mark,
        )
        self.assertEqual(self._dashboard_charge_count(account_id), 0)
        pending_billing_mark = get_sync_state(
            self.proxy_path, "dashboard_pending_billing_max_id")
        self.assertIsNotNone(pending_billing_mark)

        recovered = sync_dashboard(
            self.proxy_path, self.dashboard_path,
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(recovered["status"], "ok", recovered)
        self.assertGreater(self._dashboard_charge_count(account_id), 0)
        self.assertEqual(
            get_sync_state(self.proxy_path, "last_exported_billing_event_id"),
            pending_billing_mark,
        )

    def test_shared_receipt_blocks_old_event_after_local_mark_reset(self) -> None:
        account_id = self._create_plan()
        materialize_period_charges(
            self.proxy_path,
            datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        first = sync_dashboard(
            self.proxy_path, self.dashboard_path,
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(first["status"], "ok", first)
        self.assertGreater(self._dashboard_charge_count(account_id), 0)

        deleted = delete_dashboard_users(
            self.proxy_path, self.dashboard_path, ["immutable-plan"],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(deleted["status"], "ok", deleted)
        self.assertEqual(self._dashboard_charge_count(account_id), 0)

        # Simulate another machine whose local billing mark has not observed
        # the first export.  The shared dashboard receipt is the correctness
        # guard in this case.
        ProxyDatabase(
            self.proxy_path, schema_dir=str(self.root / "schema")
        ).set_billing_export_mark(0)
        ProxyDatabase(
            self.proxy_path, schema_dir=str(self.root / "schema")
        ).export_to_dashboard(
            self.dashboard_path, 0,
            ProxyDatabase(
                self.proxy_path, schema_dir=str(self.root / "schema")
            ).get_max_log_id(),
        )
        self.assertEqual(self._dashboard_charge_count(account_id), 0)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertGreater(conn.execute(
                "SELECT count(*) FROM billing_export_receipts"
            ).fetchone()[0], 0)


if __name__ == "__main__":
    import unittest

    unittest.main()
