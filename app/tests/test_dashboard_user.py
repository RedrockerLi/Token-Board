from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.db.dashboard_db import DashboardDatabase
from app.services.dashboard_user import (
    delete_dashboard_user_local,
    prepare_dashboard_user_delete,
    upload_dashboard_user_deletions,
)
from app.services.sync.dashboard_sync import (
    download_dashboard_from_cloud,
    upload_dashboard_to_cloud,
)
from app.services.sync.settings import SyncConfig, save_sync_config
from app.services.sync.webdav import RemoteArtifact
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
                "(month,account_id,billing_unit_id,recurring_charge,equivalent_cost) "
                "VALUES(?,?,?,?,?)",
                [("2026-08", 7, "unit-7", 3, 0),
                 ("2026-08", 8, "unit-8", 4, 0)],
            )
            conn.commit()

    def test_delete_removes_archive_locally_without_uploading(self) -> None:
        with patch("app.services.dashboard_user.upload_dashboard_to_cloud") as upload:
            result = delete_dashboard_user_local(
                str(self.proxy_path), str(self.dashboard_path), "remove-me",
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deleted_rows"], 3)
        self.assertEqual(result["account_ids"], [7])
        self.assertTrue(result["pending_upload"])
        upload.assert_not_called()

        dashboard = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema"))
        self.assertEqual(dashboard.get_account_ids_by_name("remove-me"), [])
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=7"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM monthly_recurring_costs WHERE account_id=7"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=8"
            ).fetchone()[0], 1)

    def test_first_delete_prepares_from_cloud_but_does_not_publish(self) -> None:
        save_sync_config(self.proxy_path, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))
        with patch(
                "app.services.dashboard_user.download_dashboard_from_cloud",
                return_value={"status": "ok", "remote_pulled": True}) as download:
            with patch(
                    "app.services.dashboard_user.export_dashboard_to_local",
                    return_value={"status": "ok"}) as export:
                result = delete_dashboard_user_local(
                    str(self.proxy_path), str(self.dashboard_path), "remove-me",
                    schema_dir=str(self.root / "schema"), prepare=True)

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["prepared"])
        download.assert_called_once_with(
            str(self.proxy_path), str(self.dashboard_path),
            schema_dir=str(self.root / "schema"))
        export.assert_called_once_with(
            str(self.proxy_path), str(self.dashboard_path),
            schema_dir=str(self.root / "schema"))

    def test_later_delete_is_local_only_after_first_prepare(self) -> None:
        save_sync_config(self.proxy_path, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))
        with patch(
                "app.services.dashboard_user.download_dashboard_from_cloud",
                return_value={"status": "ok"}) as download:
            with patch(
                    "app.services.dashboard_user.export_dashboard_to_local",
                    return_value={"status": "ok"}) as export:
                delete_dashboard_user_local(
                    str(self.proxy_path), str(self.dashboard_path), "remove-me",
                    schema_dir=str(self.root / "schema"), prepare=True)
                result = delete_dashboard_user_local(
                    str(self.proxy_path), str(self.dashboard_path), "keep-me",
                    schema_dir=str(self.root / "schema"), prepare=False)

        self.assertEqual(result["status"], "ok")
        download.assert_called_once()
        export.assert_called_once()

    def test_batch_deletion_uploads_local_archive_once(self) -> None:
        with patch(
                "app.services.dashboard_user.upload_dashboard_to_cloud",
                return_value={
                    "status": "ok", "message": "uploaded",
                }) as upload:
            result = upload_dashboard_user_deletions(
                str(self.proxy_path), str(self.dashboard_path),
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok")
        upload.assert_called_once_with(
            str(self.proxy_path), str(self.dashboard_path),
            schema_dir=str(self.root / "schema"),
        )

    def test_dashboard_upload_publishes_local_archive_without_download(self) -> None:
        save_sync_config(self.proxy_path, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))
        DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema")
        ).purge_accounts({7})
        uploaded = {}

        def capture_upload(_config, path, _base, _remote):
            with sqlite3.connect(path) as conn:
                uploaded["removed"] = conn.execute(
                    "SELECT count(*) FROM daily_usage WHERE account_id=7"
                ).fetchone()[0]
                uploaded["kept"] = conn.execute(
                    "SELECT count(*) FROM daily_usage WHERE account_id=8"
                ).fetchone()[0]
            return RemoteArtifact("dashboard_sync_20260825_120000.db")

        with patch("app.services.sync.dashboard_sync.latest_artifact",
                   return_value=None), patch(
                       "app.services.sync.dashboard_sync.download_artifact",
                       side_effect=AssertionError("upload must not download")), patch(
                           "app.services.sync.dashboard_sync.publish_versioned_artifact",
                           side_effect=capture_upload), patch(
                               "app.services.sync.dashboard_sync.publish_schema_manifest"):
            result = upload_dashboard_to_cloud(
                str(self.proxy_path), str(self.dashboard_path),
                schema_dir=str(self.root / "schema"),
            )

        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(uploaded, {"removed": 0, "kept": 1})
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=7"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=8"
            ).fetchone()[0], 1)

    def test_download_dashboard_does_not_upload_or_export(self) -> None:
        save_sync_config(self.proxy_path, SyncConfig(
            "https://dav.example/remote", "token-board-sync", "user", "pass"))
        with patch("app.services.sync.dashboard_sync.latest_artifact",
                   return_value=None), patch(
                       "app.services.sync.dashboard_sync.download_artifact",
                       return_value=False), patch(
                           "app.services.sync.dashboard_sync.publish_versioned_artifact",
                           side_effect=AssertionError("download must not upload")):
            result = download_dashboard_from_cloud(
                str(self.proxy_path), str(self.dashboard_path),
                schema_dir=str(self.root / "schema"))

        self.assertEqual(result["status"], "ok", result)
        self.assertFalse(result["uploaded"])
        with sqlite3.connect(self.proxy_path) as conn:
            mark = conn.execute(
                "SELECT value FROM sync_state WHERE key='last_exported_log_id'"
            ).fetchone()
        self.assertIsNone(mark)


if __name__ == "__main__":
    unittest.main()
