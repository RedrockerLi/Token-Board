from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from app.db.dashboard_db import DashboardDatabase
from app.services.sync import dashboard_sync
from app.services.sync.dashboard_sync import delete_dashboard_users
from app.services.sync.settings import SyncConfig, save_sync_config
from app.services.sync.state import get_sync_state, set_sync_state_many
from app.services.sync.webdav import RemoteArtifact, WebDAVConflict, WebDAVError
from app.services.sync.storage import safe_copy_db
from app.db.proxy_db import ProxyDatabase
from app.tests.support import AppDatabaseTestCase


class DashboardUserDeleteTest(AppDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        with sqlite3.connect(self.dashboard_path) as conn:
            conn.executemany(
                "INSERT INTO accounts(account_id,name,account_kind) VALUES(?,?,?)",
                [(7, "remove-me", "proxy"), (8, "keep-me", "proxy")],
            )
            conn.executemany(
                "INSERT INTO daily_usage"
                "(date,account_id,model,input_tokens,output_tokens,request_count) "
                "VALUES(?,?,?,?,?,?)",
                [("2026-08-01", 7, "model-a", 10, 5, 1),
                 ("2026-08-01", 8, "model-a", 20, 6, 2)],
            )
            conn.executemany(
                "INSERT INTO monthly_recurring_costs"
                "(period_start,account_id,billing_unit_id,recurring_charge,equivalent_cost) "
                "VALUES(?,?,?,?,?)",
                [("2026-08-01T00:00:00Z", 7, "unit-7", 3, 0),
                 ("2026-08-01T00:00:00Z", 8, "unit-8", 4, 0)],
            )
            conn.commit()

    def _names(self) -> list[str]:
        return DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema")
        ).load_rows()[4]

    def _configure_webdav(self) -> None:
        save_sync_config(self.proxy_path, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))

    def _local_remote_mocks(self, capture=None):
        if capture is None:
            capture = lambda _config, _path, _base, _remote: RemoteArtifact(
                "dashboard_sync_result.db")
        return patch.object(dashboard_sync, "latest_artifact", return_value=None), \
            patch.object(dashboard_sync, "download_artifact", return_value=False), \
            patch.object(dashboard_sync, "publish_versioned_artifact",
                         side_effect=capture), \
            patch.object(dashboard_sync, "publish_schema_manifest")

    def test_batch_delete_commits_one_local_candidate_without_webdav(self) -> None:
        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path),
            [" remove-me ", "remove-me", "missing"],
            schema_dir=str(self.root / "schema"),
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["deleted_names"], ["remove-me"])
        self.assertEqual(result["not_found_names"], ["missing"])
        self.assertEqual(result["deleted_rows"], 3)
        self.assertFalse(result["uploaded"])
        self.assertEqual(self._names(), ["keep-me"])

    def test_batch_delete_publishes_the_mutated_candidate(self) -> None:
        self._configure_webdav()
        published_rows = []

        def capture(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                published_rows.append({
                    "remove": conn.execute(
                        "SELECT count(*) FROM accounts WHERE name='remove-me'"
                    ).fetchone()[0],
                    "keep": conn.execute(
                        "SELECT count(*) FROM accounts WHERE name='keep-me'"
                    ).fetchone()[0],
                })
            return RemoteArtifact("dashboard_sync_result.db")

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["uploaded"])
        self.assertEqual(published_rows, [{"remove": 0, "keep": 1}])
        self.assertEqual(self._names(), ["keep-me"])
        self.assertIsNone(get_sync_state(
            str(self.proxy_path), "dashboard_pending_path"))

    def test_recovery_continues_current_delete_instead_of_returning_early(self) -> None:
        self._configure_webdav()
        pending = dashboard_sync._pending_dashboard_path(str(self.dashboard_path))
        safe_copy_db(str(self.dashboard_path), pending)
        DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema")
        ).purge_accounts({7})
        set_sync_state_many(str(self.proxy_path), {
            "dashboard_pending_path": pending,
            "dashboard_pending_export_max_id": "0",
            "dashboard_pending_remote_artifact": "",
            "dashboard_pending_remote_etag": "",
        })
        published_rows = []

        def capture(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                published_rows.append(conn.execute(
                    "SELECT count(*) FROM accounts WHERE name='remove-me'"
                ).fetchone()[0])
            return RemoteArtifact(f"dashboard_sync_{len(published_rows)}.db")

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(published_rows, [1, 0])
        self.assertEqual(self._names(), ["keep-me"])
        self.assertFalse(os.path.exists(pending))

    def test_upload_failure_keeps_formal_archive_and_pending_candidate(self) -> None:
        self._configure_webdav()
        patches = self._local_remote_mocks(
            capture=lambda *_args: (_ for _ in ()).throw(WebDAVError("offline")))
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "error", result)
        self.assertEqual(self._names(), ["keep-me", "remove-me"])
        pending = get_sync_state(str(self.proxy_path), "dashboard_pending_path")
        self.assertTrue(pending and os.path.exists(pending))
        self.assertEqual(
            DashboardDatabase(pending, str(self.root / "schema")
                              ).get_account_ids_by_name("remove-me"), [])

    def test_conflict_rebuilds_candidate_and_reapplies_delete(self) -> None:
        self._configure_webdav()
        published_rows = []
        responses = [WebDAVConflict("race"), RemoteArtifact("result.db")]

        def capture(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                published_rows.append(conn.execute(
                    "SELECT count(*) FROM accounts WHERE name='remove-me'"
                ).fetchone()[0])
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(published_rows, [0, 0])
        self.assertEqual(self._names(), ["keep-me"])

    def test_all_missing_users_do_not_create_or_publish_archive(self) -> None:
        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path), ["missing"],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["not_found_names"], ["missing"])
        self.assertEqual(self._names(), ["keep-me", "remove-me"])
        self.assertIsNone(get_sync_state(
            str(self.proxy_path), "dashboard_pending_path"))

    def test_concurrent_deletes_are_serialized(self) -> None:
        def delete_once():
            return delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
                schema_dir=str(self.root / "schema"),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: delete_once(), (1, 2)))
        self.assertEqual({result["status"] for result in results},
                         {"ok", "not_found"})
        self.assertEqual(self._names(), ["keep-me"])

    def test_deleted_account_can_accept_later_dashboard_charge(self) -> None:
        dashboard = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema"))
        dashboard.purge_accounts({7})
        self.assertEqual(dashboard.upsert_account_batch([{
            "account_id": 7, "name": "remove-me",
            "updated_at": "2026-01-01T00:00:00Z",
            "account_kind": "proxy",
        }]), 1)
        self.assertEqual(dashboard.upsert_frozen_plan_charge(
            month="2026-08", account_id=7, billing_unit_id="unit-7",
            recurring_charge=20, normalized_recurring_cost=20,
            currency="CNY", base_currency="CNY", fx_rate_date=None,
            frozen_at="2026-08-01T00:00:00Z"), 1)
        self.assertCountEqual(self._names(), ["keep-me", "remove-me"])

    def test_deleted_account_can_accept_later_usage_export(self) -> None:
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(7,'remove-me-account','remove-me','agent')"
            )

        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path), ["remove-me"],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(result["status"], "ok", result)

        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,source_kind,account_id,model,prompt_tokens,"
                "completion_tokens,cache_read_tokens,total_tokens,equivalent_cost,"
                "billed_usage_cost,status_code,requested_at) "
                "VALUES('remove-me-new-usage','import',7,'new-model',10,5,0,15,"
                "1.25,1.00,200,'2026-09-06T00:00:00Z')"
            )
            max_id = conn.execute("SELECT MAX(id) FROM request_log").fetchone()[0]

        ProxyDatabase(
            str(self.proxy_path), schema_dir=str(self.root / "schema")
        ).export_to_dashboard(str(self.dashboard_path), 0, max_id)
        self.assertCountEqual(self._names(), ["keep-me", "remove-me"])

    def test_opencode_zen_and_lm_studio_do_not_return_after_export(self) -> None:
        with sqlite3.connect(self.dashboard_path) as conn:
            conn.executemany(
                "INSERT INTO accounts(account_id,name,account_kind) VALUES(?,?,?)",
                [(14, "OpenCode Zen", "proxy"), (17, "LM studio", "proxy")],
            )
            conn.executemany(
                "INSERT INTO monthly_recurring_costs"
                "(period_start,account_id,billing_unit_id,recurring_charge,"
                "equivalent_cost,normalized_recurring_cost,charge_frozen_at) "
                "VALUES('2026-08-01T00:00:00Z',?,?,0,0,0,?)",
                [(14, "zen-unit", "2026-08-15T00:00:00Z"),
                 (17, "lm-unit", "2026-08-18T00:00:00Z")],
            )
            conn.commit()
        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path),
            ["OpenCode Zen", "LM studio"],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(result["status"], "ok", result)
        ProxyDatabase(
            str(self.proxy_path), schema_dir=str(self.root / "schema")
        ).export_to_dashboard(str(self.dashboard_path), 0, 0)
        self.assertNotIn("OpenCode Zen", self._names())
        self.assertNotIn("LM studio", self._names())

    def test_frozen_dashboard_charge_cannot_be_overwritten(self) -> None:
        dashboard = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema"))
        self.assertEqual(dashboard.upsert_frozen_plan_charge(
            month="2026-08", account_id=8, billing_unit_id="unit-8",
            recurring_charge=10, normalized_recurring_cost=10,
            currency="CNY", base_currency="CNY", fx_rate_date=None,
            frozen_at="2026-08-01T00:00:00Z"), 1)
        self.assertEqual(dashboard.upsert_frozen_plan_charge(
            month="2026-08", account_id=8, billing_unit_id="unit-8",
            recurring_charge=20, normalized_recurring_cost=20,
            currency="CNY", base_currency="CNY", fx_rate_date=None,
            frozen_at="2026-08-02T00:00:00Z"), 0)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT recurring_charge,normalized_recurring_cost "
                "FROM monthly_recurring_costs WHERE account_id=8 "
                "AND billing_unit_id='unit-8'"
            ).fetchone(), (10.0, 10.0))

    def test_zero_price_period_stays_in_source_but_not_dashboard(self) -> None:
        proxy = self.proxy_database()
        account_id = proxy.create_account({
            "name": "free-plan", "account_type": "plan",
            "currency": "CNY", "monthly_price": 0,
            "valid_from": "2026-07-01", "base_url": "http://example.test",
            "upstream_keys": ["sk-free-plan"],
            "new_valid_froms": ["2026-07-01"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_rate_events SET effective_at='1990-01-01T00:00:00Z'"
            )
            conn.commit()
        source = ProxyDatabase(
            str(self.proxy_path), schema_dir=str(self.root / "schema"))
        source.export_to_dashboard(str(self.dashboard_path), 0, 0)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertGreater(conn.execute(
                "SELECT count(*) FROM billing_period_charges "
                "WHERE contract_id IN (SELECT id FROM billing_contracts WHERE account_id=?)",
                (account_id,)
            ).fetchone()[0], 0)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM monthly_recurring_costs WHERE account_id=?",
                (account_id,)
            ).fetchone()[0], 0)
        self.assertNotIn("free-plan", self._names())


if __name__ == "__main__":
    unittest.main()
