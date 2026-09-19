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
                "INSERT INTO users(id,name,actual_cost_micro_cny) VALUES(?,?,?)",
                [(7, "remove-me", 3_000_000), (8, "keep-me", 4_000_000)],
            )
            conn.executemany(
                "INSERT INTO daily_model_usage"
                "(user_id,usage_date,model,input_tokens,cache_read_tokens,"
                "output_tokens,request_count,api_equivalent_cost_micro_cny) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(7, "2026-08-01", "model-a", 10, 0, 5, 1, 0),
                 (8, "2026-08-01", "model-a", 20, 0, 6, 2, 0)],
            )
            conn.commit()

    def _names(self) -> list[str]:
        users = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema")
        ).load_rows()[8]
        return [user["name"] for user in users if user["id"] != 0]

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
            [7, 7, 999],
            schema_dir=str(self.root / "schema"),
        )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["archived_user_ids"], [7])
        self.assertEqual(result["not_found_user_ids"], [999])
        self.assertEqual(result["archived_rows"], 1)
        self.assertFalse(result["uploaded"])
        self.assertEqual(self._names(), ["keep-me"])

    def test_archive_does_not_export_or_clean_local_usage(self) -> None:
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(7,'archive-agent','remove-me','agent')"
            )
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,source_kind,account_id,agent_software_id,model,"
                "prompt_tokens,completion_tokens,cache_read_tokens,total_tokens,"
                "equivalent_cost,billed_usage_cost,status_code,requested_at) "
                "VALUES('archive-pending','import',7,7,'old-model',10,5,0,15,"
                "1.25,0,200,'2020-01-01T00:00:00Z')"
            )
            conn.commit()

        with patch.object(
                dashboard_sync, "_export_dashboard",
                side_effect=AssertionError("archive must not export usage")), \
                patch("app.db.proxy.billing.materialize_all_period_charges",
                      side_effect=AssertionError("archive must not materialize billing")), \
                patch.object(
                    ProxyDatabase, "set_export_marks",
                    side_effect=AssertionError("archive must not advance export marks")), \
                patch.object(
                    ProxyDatabase, "cleanup_exported_logs",
                    side_effect=AssertionError("archive must not clean logs")):
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), [7],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM request_log "
                    "WHERE event_id='archive-pending'"
                ).fetchone()[0],
                1,
            )
            self.assertIsNone(conn.execute(
                "SELECT value FROM sync_state "
                "WHERE key='last_exported_log_id'"
            ).fetchone())
        self.assertCountEqual(self._names(), ["keep-me"])

    def test_archive_does_not_resume_an_old_export_pending(self) -> None:
        pending = dashboard_sync._pending_dashboard_path(str(self.dashboard_path))
        safe_copy_db(str(self.dashboard_path), pending)
        set_state = {
            "dashboard_pending_path": pending,
            # No operation marker: legacy pending state is an export.
            "dashboard_pending_export_max_id": "17",
            "dashboard_pending_remote_artifact": "",
            "dashboard_pending_remote_etag": "",
        }
        set_sync_state_many(str(self.proxy_path), set_state)

        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path), [7],
            schema_dir=str(self.root / "schema"),
        )

        self.assertEqual(result["status"], "conflict", result)
        self.assertTrue(os.path.exists(pending))
        self.assertCountEqual(self._names(), ["keep-me", "remove-me"])

    def test_batch_delete_publishes_the_mutated_candidate(self) -> None:
        self._configure_webdav()
        published_rows = []

        def capture(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                published_rows.append({
                    "remove": conn.execute(
                        "SELECT count(*) FROM users WHERE name='remove-me'"
                    ).fetchone()[0],
                    "keep": conn.execute(
                        "SELECT count(*) FROM users WHERE name='keep-me'"
                    ).fetchone()[0],
                })
            return RemoteArtifact("dashboard_sync_result.db")

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), [7],
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
            "dashboard_pending_operation": "archive",
            "dashboard_pending_export_max_id": "0",
            "dashboard_pending_remote_artifact": "",
            "dashboard_pending_remote_etag": "",
        })
        published_rows = []

        def capture(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                published_rows.append(conn.execute(
                    "SELECT count(*) FROM users WHERE name='remove-me'"
                ).fetchone()[0])
            return RemoteArtifact(f"dashboard_sync_{len(published_rows)}.db")

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), [7],
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
                str(self.proxy_path), str(self.dashboard_path), [7],
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
                    "SELECT count(*) FROM users WHERE name='remove-me'"
                ).fetchone()[0])
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        patches = self._local_remote_mocks(capture=capture)
        with patches[0], patches[1], patches[2], patches[3]:
            result = delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), [7],
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(published_rows, [0, 0])
        self.assertEqual(self._names(), ["keep-me"])

    def test_all_missing_users_do_not_create_or_publish_archive(self) -> None:
        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path), [999],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["not_found_user_ids"], [999])
        self.assertEqual(self._names(), ["keep-me", "remove-me"])
        self.assertIsNone(get_sync_state(
            str(self.proxy_path), "dashboard_pending_path"))

    def test_concurrent_deletes_are_serialized(self) -> None:
        def delete_once():
            return delete_dashboard_users(
                str(self.proxy_path), str(self.dashboard_path), [7],
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
            frozen_on="2026-08-01"), 1)
        self.assertCountEqual(self._names(), ["keep-me", "remove-me"])

    def test_deleted_account_can_accept_later_usage_export(self) -> None:
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO accounts(id,uuid,name,account_kind) "
                "VALUES(7,'remove-me-account','remove-me','agent')"
            )

        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path), [7],
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
                "INSERT INTO users(id,name,actual_cost_micro_cny) VALUES(?,?,?)",
                [(14, "OpenCode Zen", 0), (17, "LM studio", 0)],
            )
            conn.commit()
        result = delete_dashboard_users(
            str(self.proxy_path), str(self.dashboard_path),
            [14, 17],
            schema_dir=str(self.root / "schema"),
        )
        self.assertEqual(result["status"], "ok", result)
        ProxyDatabase(
            str(self.proxy_path), schema_dir=str(self.root / "schema")
        ).export_to_dashboard(str(self.dashboard_path), 0, 0)
        self.assertNotIn("OpenCode Zen", self._names())
        self.assertNotIn("LM studio", self._names())

    def test_frozen_dashboard_charges_accumulate_on_user_identity(self) -> None:
        dashboard = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema"))
        self.assertEqual(dashboard.upsert_frozen_plan_charge(
            month="2026-08", account_id=8, billing_unit_id="unit-8",
            recurring_charge=10, normalized_recurring_cost=10,
            currency="CNY", base_currency="CNY", fx_rate_date=None,
            frozen_on="2026-08-01"), 1)
        self.assertEqual(dashboard.upsert_frozen_plan_charge(
            month="2026-08", account_id=8, billing_unit_id="unit-8",
            recurring_charge=20, normalized_recurring_cost=20,
            currency="CNY", base_currency="CNY", fx_rate_date=None,
            frozen_on="2026-08-02"), 1)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT actual_cost_micro_cny FROM users WHERE id=8"
            ).fetchone(), (34_000_000,))

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
                "SELECT count(*) FROM users WHERE id=?",
                (account_id,)
            ).fetchone()[0], 0)
        self.assertNotIn("free-plan", self._names())


if __name__ == "__main__":
    unittest.main()
