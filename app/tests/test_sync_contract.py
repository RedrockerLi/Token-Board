from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.migrations import SchemaVersion, apply_sql_migrations, migrate
from app.db.proxy_db import ProxyDatabase
from app.db.schema_upgrade.coordinator import inspect_version
from app.services.sync.settings import SyncConfig, save_sync_config
from app.services.sync.webdav import (
    ArtifactTransaction,
    RemoteArtifact,
    WebDAVConflict,
    WebDAVClient,
    publish_versioned_artifact,
)
from app.services.sync.config_merge import merge_config_tables
from app.services.sync.config_sync import sync_config_upload
from app.services.sync.dashboard_sync import sync_dashboard
from app.services.sync.state import config_hash_of_db

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SyncContractTest(unittest.TestCase):
    def _seed_credential(self, db_path: str, credential_uuid: str,
                         secret: str | None, disabled_at: str | None = None
                         ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO accounts(id,uuid,name) "
                "VALUES(1,'acct-1','local')")
            conn.execute(
                "INSERT OR IGNORE INTO upstreams(id,account_id,name,base_url,enabled) "
                "VALUES(1,1,'upstream','http://example.test',1)")
            conn.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,"
                "position,key_masked,disabled_at) VALUES(?,1,1,0,'sk-local',?)",
                (credential_uuid, disabled_at))
            if secret is not None:
                conn.execute(
                    "INSERT INTO upstream_secrets(credential_uuid,secret_value) "
                    "VALUES(?,?)", (credential_uuid, secret))
            conn.commit()
        finally:
            conn.close()

    def test_cloud_merge_preserves_local_secrets_but_syncs_metadata(self) -> None:
        temp = tempfile.mkdtemp()
        try:
            shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
            local = str(Path(temp) / "local.db")
            remote = str(Path(temp) / "remote.db")
            migrate(local, str(Path(temp) / "schema"), "token-board")
            migrate(remote, str(Path(temp) / "schema"), "token-board")
            self._seed_credential(local, "credential-local",
                                  "sk-local-secret")
            # Remote does not know this credential at all.
            merge_config_tables(remote, local)
            conn = sqlite3.connect(local)
            row = conn.execute(
                "SELECT disabled_at FROM upstream_credentials "
                "WHERE uuid='credential-local'").fetchone()
            secret = conn.execute(
                "SELECT secret_value FROM upstream_secrets "
                "WHERE credential_uuid='credential-local'").fetchone()
            conn.close()
            self.assertIsNotNone(row[0])
            self.assertEqual(secret[0], "sk-local-secret")

            # A remote disabled timestamp is configuration state and is
            # authoritative in the new sync contract.
            self._seed_credential(remote, "credential-local", None,
                                  "2026-08-10T04:01:36Z")
            merge_config_tables(remote, local)
            conn = sqlite3.connect(local)
            row = conn.execute(
                "SELECT disabled_at FROM upstream_credentials "
                "WHERE uuid='credential-local'").fetchone()
            secret = conn.execute(
                "SELECT secret_value FROM upstream_secrets "
                "WHERE credential_uuid='credential-local'").fetchone()
            conn.close()
            self.assertEqual(row[0], "2026-08-10T04:01:36Z")
            self.assertEqual(secret[0], "sk-local-secret")
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_cloud_merge_does_not_resurrect_historical_current_pricing_rate(self) -> None:
        """A stale cloud current rate must not coexist with a newer local one."""
        temp = tempfile.mkdtemp()
        try:
            shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
            local = str(Path(temp) / "local.db")
            remote = str(Path(temp) / "remote.db")
            migrate(local, str(Path(temp) / "schema"), "token-board")
            migrate(remote, str(Path(temp) / "schema"), "token-board")
            for path in (local, remote):
                conn = sqlite3.connect(path)
                conn.execute(
                    "INSERT INTO pricing_rules(id,model_pattern,priority) "
                    "VALUES(1,'merge-demo',0)"
                )
                conn.execute(
                    "INSERT INTO pricing_rates"
                    "(id,pricing_rule_id,input_price,cache_read_price,"
                    "output_price,currency,valid_from) "
                    "VALUES(1,1,1,0.1,2,'USD','2026-01-01T00:00:00Z')"
                )
                conn.commit()
                conn.close()

            local_db = ProxyDatabase(local, str(Path(temp) / "schema"))
            self.assertTrue(local_db.update_pricing(1, {
                "input_price": 3,
                "cache_read_price": 0.3,
                "output_price": 4,
                "currency": "USD",
            }))

            merge_config_tables(remote, local)

            with sqlite3.connect(local) as conn:
                current = conn.execute(
                    "SELECT id,input_price,output_price FROM pricing_rates "
                    "WHERE pricing_rule_id=1 AND valid_until IS NULL "
                    "ORDER BY id"
                ).fetchall()
            # Configuration downloads are cloud-authoritative: the remote
            # current version wins, but it must not coexist with local history.
            self.assertEqual(current, [(1, 1.0, 2.0)])
            self.assertEqual(len(local_db.get_pricing()), 1)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_pricing_current_rate_migration_repairs_existing_duplicates(self) -> None:
        temp = tempfile.mkdtemp()
        try:
            shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
            local = str(Path(temp) / "local.db")
            schema = str(Path(temp) / "schema")
            apply_sql_migrations(
                local, schema, "token-board", SchemaVersion(1, 12))
            with sqlite3.connect(local) as conn:
                conn.execute(
                    "INSERT INTO pricing_rules(id,model_pattern,priority) "
                    "VALUES(1,'migration-demo',0)"
                )
                conn.executemany(
                    "INSERT INTO pricing_rates"
                    "(id,pricing_rule_id,input_price,cache_read_price,"
                    "output_price,currency,valid_from) VALUES(?,?,?,?,?,?,?)",
                    [
                        (1, 1, 1, 0.1, 2, "USD", "2026-01-01T00:00:00Z"),
                        (2, 1, 3, 0.3, 4, "USD", "2026-02-01T00:00:00Z"),
                    ],
                )
            migrate(local, schema, "token-board")
            with sqlite3.connect(local) as conn:
                current = conn.execute(
                    "SELECT id FROM pricing_rates WHERE pricing_rule_id=1 "
                    "AND valid_until IS NULL"
                ).fetchall()
                indexes = [row[1] for row in conn.execute(
                    "PRAGMA index_list(pricing_rates)")]
            self.assertEqual(current, [(2,)])
            self.assertIn("idx_pricing_rates_one_current", indexes)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_hash_excludes_secrets_and_webdav_password(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            self._seed_credential(proxy, "credential-local", "sk-local-secret")
            before = config_hash_of_db(proxy)
            conn = sqlite3.connect(proxy)
            conn.execute(
                "UPDATE upstream_secrets SET secret_value='sk-changed' "
                "WHERE credential_uuid='credential-local'")
            conn.execute(
                "UPDATE sync_settings SET value='another-password' WHERE key='password'")
            conn.commit()
            conn.close()
            self.assertEqual(before, config_hash_of_db(proxy))
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_versioned_upload_refuses_stale_remote_etag(self) -> None:
        config = SyncConfig("https://dav.example", "token-board", "u", "p")
        expected = RemoteArtifact("token-board_config_20260809_010000.db", etag='"old"')
        current = RemoteArtifact("token-board_config_20260809_010001.db", etag='"new"')
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "config.db"
            source.write_bytes(b"fixture")
            class FakeClient(WebDAVClient):
                def __init__(self):
                    super().__init__(config)

                def list_artifacts(self, prefix):
                    return [current] if current.name.startswith(prefix) else []

                def upload_artifact(self, source, name, **kwargs):
                    raise AssertionError(
                        "stale artifact must be rejected before PUT")

            with self.assertRaises(WebDAVConflict):
                ArtifactTransaction(FakeClient()).publish_versioned_artifact(
                    str(source), "token-board_config", expected)

    def test_dashboard_retries_complete_transaction_after_upload_race(self) -> None:
        with patch("app.services.sync.dashboard_sync._sync_dashboard_once",
                   side_effect=[WebDAVConflict("race-1"),
                                WebDAVConflict("race-2"),
                                {"status": "ok"}]) as once:
            result = sync_dashboard("token-board.db", "dashboard.db")
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(once.call_count, 3)

    def test_config_upload_retries_complete_transaction_after_upload_race(self) -> None:
        with patch("app.services.sync.config_sync._sync_config_upload_once",
                   side_effect=[WebDAVConflict("race-1"),
                                WebDAVConflict("race-2"),
                                {"status": "ok"}]) as once:
            result = sync_config_upload("token-board.db")
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(once.call_count, 3)

    def test_config_upload_refreshes_cloud_on_hash_conflict(self) -> None:
        conflict = {
            "status": "conflict",
            "message": "cloud changed",
            "conflict": True,
        }
        with patch("app.services.sync.config_sync._sync_config_upload_once",
                   return_value=conflict) as once, patch(
                       "app.services.sync.config_sync.sync_config_download",
                       return_value=True) as download:
            result = sync_config_upload("token-board.db")

        self.assertEqual(result["status"], "remote_updated")
        self.assertEqual(result["message"],
                         "云端配置已更新，本机修改已丢弃，请重新设置。")
        once.assert_called_once_with("token-board.db", schema_dir=None)
        download.assert_called_once_with("token-board.db", schema_dir=None)

    def test_config_upload_keeps_recovery_conflict_when_refresh_fails(self) -> None:
        conflict = {
            "status": "conflict",
            "message": "cloud changed",
            "conflict": True,
        }
        with patch("app.services.sync.config_sync._sync_config_upload_once",
                   return_value=conflict), patch(
                       "app.services.sync.config_sync.sync_config_download",
                       return_value=False) as download:
            result = sync_config_upload("token-board.db")

        self.assertEqual(result["status"], "conflict")
        self.assertIn("自动拉取失败", result["message"])
        download.assert_called_once_with("token-board.db", schema_dir=None)

    def test_config_upload_refreshes_after_repeated_upload_race(self) -> None:
        with patch("app.services.sync.config_sync._sync_config_upload_once",
                   side_effect=[WebDAVConflict("race-1"),
                                WebDAVConflict("race-2"),
                                WebDAVConflict("race-3")]) as once, patch(
                       "app.services.sync.config_sync.sync_config_download",
                       return_value=True) as download:
            result = sync_config_upload("token-board.db")

        self.assertEqual(result["status"], "remote_updated")
        self.assertEqual(once.call_count, 3)
        download.assert_called_once_with("token-board.db", schema_dir=None)

    def _repo_layout(self) -> tuple[str, str, str]:
        """Temp dir laid out like the repo (schema/ + data/) so that
        ``schema_dir_for`` resolves to a real schema root."""
        temp = tempfile.mkdtemp()
        shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
        (Path(temp) / "data").mkdir()
        proxy = str(Path(temp) / "data" / "token-board.db")
        dash = str(Path(temp) / "data" / "dashboard.db")
        migrate(proxy, str(Path(temp) / "schema"), "token-board")
        migrate(dash, str(Path(temp) / "schema"), "dashboard")
        save_sync_config(proxy, SyncConfig(
            "https://dav.example/remote.php/dav/files/u",
            "token-board-sync", "user", "pass"))
        return temp, proxy, dash

    def _webdav_mocks(self):
        """Disable the network for a full local sync transaction.

        Patch the public transport boundary at each workflow's lookup site.
        Uploads return a confirmed artifact so the rest of the workflow can
        exercise its local commit behavior without network I/O.
        """
        for module in ("app.services.sync.config_sync",
                       "app.services.sync.dashboard_sync"):
            patch(module + ".latest_artifact", return_value=None).start()
            patch(module + ".download_artifact", return_value=False).start()
            patch(module + ".publish_versioned_artifact",
                  return_value=RemoteArtifact(
                      "token-board_config_20260824_120000.db",
                      etag='"etag"')).start()
            patch(module + ".publish_schema_manifest").start()

    def test_config_upload_records_remote_metadata(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            self._webdav_mocks()
            result = sync_config_upload(proxy)
            self.assertEqual(result["status"], "ok", result)
            conn = sqlite3.connect(proxy)
            rows = dict(conn.execute(
                "SELECT key, value FROM sync_state").fetchall())
            conn.close()
            self.assertIn("token-board_remote_sha256", rows)
            self.assertEqual(len(rows["token-board_remote_sha256"]), 64)
            self.assertEqual(rows["token-board_remote_major"], "1")
            version = inspect_version(Path(proxy), "token-board")
            self.assertEqual(rows["token-board_remote_minor"], str(version.minor))
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_upload_strips_sensitive_values_but_keeps_client_keys(self) -> None:
        temp, proxy, _ = self._repo_layout()
        captured: dict[str, object] = {}
        try:
            self._seed_credential(proxy, "credential-local", "sk-local-secret")
            conn = sqlite3.connect(proxy)
            conn.execute(
                "INSERT INTO route_sets(id,uuid,account_id,name) "
                "VALUES(1,'route-1',1,'local')")
            conn.execute(
                "INSERT INTO client_keys(uuid,key_value,label,route_set_id) "
                "VALUES('client-1','tb-local-key','local key',1)")
            conn.commit()
            conn.close()

            self._webdav_mocks()

            def capture(config, path, base, remote_artifact):
                check = sqlite3.connect(path)
                captured["secret_count"] = check.execute(
                    "SELECT count(*) FROM upstream_secrets").fetchone()[0]
                captured["password"] = check.execute(
                    "SELECT value FROM sync_settings WHERE key='password'").fetchone()
                captured["client_key"] = check.execute(
                    "SELECT key_value FROM client_keys WHERE uuid='client-1'").fetchone()[0]
                check.close()
                return RemoteArtifact("token-board_config_20260824_120000.db",
                                      etag='"etag"')

            with patch("app.services.sync.config_sync.publish_versioned_artifact",
                       side_effect=capture):
                result = sync_config_upload(proxy)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(captured["secret_count"], 0)
            self.assertIsNone(captured["password"])
            self.assertEqual(captured["client_key"], "tb-local-key")

            conn = sqlite3.connect(proxy)
            self.assertEqual(
                conn.execute("SELECT secret_value FROM upstream_secrets").fetchone()[0],
                "sk-local-secret")
            self.assertEqual(
                conn.execute("SELECT value FROM sync_settings WHERE key='password'").fetchone()[0],
                "pass")
            conn.close()
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_dashboard_sync_records_remote_metadata(self) -> None:
        temp, proxy, dash = self._repo_layout()
        try:
            self._webdav_mocks()
            result = sync_dashboard(proxy, dash)
            self.assertEqual(result["status"], "ok", result)
            conn = sqlite3.connect(proxy)
            rows = dict(conn.execute(
                "SELECT key, value FROM sync_state").fetchall())
            conn.close()
            self.assertIn("dashboard_remote_sha256", rows)
            self.assertEqual(len(rows["dashboard_remote_sha256"]), 64)
            self.assertEqual(rows["dashboard_remote_major"], "1")
            version = inspect_version(Path(dash), "dashboard")
            self.assertEqual(rows["dashboard_remote_minor"], str(version.minor))
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_dashboard_sync_without_webdav_commits_locally(self) -> None:
        temp = tempfile.mkdtemp()
        try:
            shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
            (Path(temp) / "data").mkdir()
            proxy = str(Path(temp) / "data" / "token-board.db")
            dash = str(Path(temp) / "data" / "dashboard.db")
            migrate(proxy, str(Path(temp) / "schema"), "token-board")
            migrate(dash, str(Path(temp) / "schema"), "dashboard")

            result = sync_dashboard(
                proxy, dash, schema_dir=str(Path(temp) / "schema"))

            self.assertEqual(result["status"], "ok", result)
            self.assertFalse(result["uploaded"])
            with sqlite3.connect(proxy) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM sync_state "
                        "WHERE key='last_exported_log_id'"
                    ).fetchone()[0], "0")
        finally:
            shutil.rmtree(temp, ignore_errors=True)
