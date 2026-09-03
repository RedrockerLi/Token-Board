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
    WebDAVError,
    WebDAVClient,
    publish_versioned_artifact,
)
from app.services.sync.config_merge import merge_config_tables
from app.services.sync.config_sync import sync_config_pull, sync_config_upload
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

    def test_cloud_merge_uses_flat_pricing_and_deletes_missing_rules(self) -> None:
        """Cloud pricing is current-only and absent rules are hard-deleted."""
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
                    "INSERT INTO pricing_rules"
                    "(id,model_pattern,priority,input_price,cache_read_price,"
                    "output_price,currency) VALUES(1,'merge-demo',0,1,0.1,2,'USD')"
                )
                conn.commit()
                conn.close()

            local_db = ProxyDatabase(local, str(Path(temp) / "schema"))
            with sqlite3.connect(local) as conn:
                conn.execute(
                    "INSERT INTO pricing_rules"
                    "(id,model_pattern,priority,input_price,cache_read_price,"
                    "output_price,currency) VALUES(2,'local-only',1,9,9,9,'CNY')"
                )
                conn.commit()
            self.assertTrue(local_db.update_pricing(1, {
                "input_price": 3,
                "cache_read_price": 0.3,
                "output_price": 4,
                "currency": "USD",
            }))

            merge_config_tables(remote, local)

            with sqlite3.connect(local) as conn:
                current = conn.execute(
                    "SELECT id,input_price,output_price FROM pricing_rules "
                    "WHERE id=1"
                ).fetchall()
            # Configuration downloads are cloud-authoritative: the remote
            # current value wins and a local-only rule is physically removed.
            self.assertEqual(current, [(1, 1.0, 2.0)])
            with sqlite3.connect(local) as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM pricing_rules WHERE id=2"
                ).fetchone()[0], 0)
            self.assertEqual(len(local_db.get_pricing()), 1)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_pricing_current_only_artifact_migration_preserves_costs(self) -> None:
        temp = tempfile.mkdtemp()
        try:
            shutil.copytree(str(_REPO_ROOT / "schema"), Path(temp) / "schema")
            local = str(Path(temp) / "local.db")
            schema = str(Path(temp) / "schema")
            apply_sql_migrations(
                local, schema, "token-board", SchemaVersion(1, 13))
            with sqlite3.connect(local) as conn:
                conn.execute(
                    "INSERT INTO pricing_rules(id,model_pattern,priority) "
                    "VALUES(1,'migration-demo',0)"
                )
                conn.executemany(
                    "INSERT INTO pricing_rates"
                    "(id,pricing_rule_id,input_price,cache_read_price,"
                    "output_price,currency,valid_from,valid_until) VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (1, 1, 1, 0.1, 2, "CNY", "2026-01-01T00:00:00Z", None),
                        (2, 1, 3, 0.3, 4, "CNY", "2026-02-01T00:00:00Z",
                         "2026-03-01T00:00:00Z"),
                    ],
                )
                conn.execute(
                    "INSERT INTO pricing_rules(id,model_pattern,priority) "
                    "VALUES(2,'disabled-rule',1)"
                )
                conn.execute(
                    "INSERT INTO pricing_rates"
                    "(id,pricing_rule_id,input_price,cache_read_price,"
                    "output_price,currency,valid_from) VALUES(3,2,8,8,8,'CNY',"
                    "'2026-01-01T00:00:00Z')"
                )
                conn.execute("UPDATE pricing_rules SET enabled=0 WHERE id=2")
                conn.commit()
            before = sqlite3.connect(local).execute(
                "SELECT count(*),sum(equivalent_cost),sum(billed_usage_cost) FROM request_log"
            ).fetchone()
            from app.db.schema_upgrade import upgrade_downloaded_artifact
            upgrade_downloaded_artifact(local, "token-board", schema)
            with sqlite3.connect(local) as conn:
                self.assertEqual(conn.execute(
                    "SELECT id,input_price,output_price FROM pricing_rules"
                ).fetchall(), [(1, 1.0, 2.0)])
                self.assertFalse(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='pricing_rates'"
                ).fetchone())
                self.assertEqual(conn.execute(
                    "SELECT count(*),sum(equivalent_cost),sum(billed_usage_cost) FROM request_log"
                ).fetchone(), before)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM schema_transitions "
                    "WHERE transition_id='v1-pricing-current-only'"
                ).fetchone()[0], 1)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_pull_republishes_upgraded_pricing_artifact_once(self) -> None:
        temp, proxy, _ = self._repo_layout()
        schema = str(Path(temp) / "schema")
        remote_path = Path(temp) / "remote-v113.db"
        published_path = Path(temp) / "remote-v114.db"
        apply_sql_migrations(
            str(remote_path), schema, "token-board", SchemaVersion(1, 13))
        with sqlite3.connect(remote_path) as conn:
            conn.execute(
                "INSERT INTO pricing_rules(id,model_pattern,priority) "
                "VALUES(1,'remote-model',0)"
            )
            conn.execute(
                "INSERT INTO pricing_rates"
                "(id,pricing_rule_id,input_price,cache_read_price,output_price,"
                "currency,valid_from) VALUES(1,1,1,0.5,2,'CNY',"
                "'2020-01-01T00:00:00Z')"
            )
            conn.commit()

        state = {"artifact": RemoteArtifact(
            "token-board_config_20260801_000000.db"),
            "path": remote_path}

        def download(_config, destination, remote_filename=None):
            del remote_filename
            shutil.copy2(state["path"], destination)
            return True

        def publish(_config, source):
            shutil.copy2(source, published_path)
            state["artifact"] = RemoteArtifact(
                "token-board_config_20260903_000000.db.gz")
            state["path"] = published_path
            with sqlite3.connect(source) as conn:
                self.assertFalse(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='pricing_rates'"
                ).fetchone())
                self.assertEqual(conn.execute(
                    "SELECT major,minor FROM schema_version WHERE id=1"
                ).fetchone(), (1, 14))
            return state["artifact"]

        try:
            with patch("app.services.sync.config_sync.latest_artifact",
                       side_effect=lambda _config, _base: state["artifact"]), \
                 patch("app.services.sync.config_sync.download_artifact",
                       side_effect=download), \
                 patch("app.services.sync.config_sync.publish_config_artifact",
                       side_effect=publish) as publish_mock:
                first = sync_config_pull(proxy, schema_dir=schema)
                second = sync_config_pull(proxy, schema_dir=schema)
            self.assertEqual(first["status"], "pulled", first)
            self.assertEqual(second["status"], "pulled", second)
            self.assertEqual(publish_mock.call_count, 1)
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

    def test_config_upload_uses_one_put_without_conflict_roundtrip(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            self._webdav_mocks()
            with patch("app.services.sync.config_sync.publish_config_artifact",
                       return_value=RemoteArtifact(
                           "token-board_config_20260824_120000.db")) as publish:
                result = sync_config_upload(proxy)
            self.assertEqual(result["status"], "ok", result)
            publish.assert_called_once()
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_upload_with_frozen_agent_charge_allocations_succeeds(self) -> None:
        temp, proxy, _ = self._repo_layout()
        captured: dict[str, object] = {}
        try:
            from app.services.sync.snapshot import snapshot_config

            with sqlite3.connect(proxy) as conn:
                conn.execute(
                    "INSERT INTO accounts(id,uuid,name) VALUES(1,'acct','agent')")
                conn.execute(
                    "INSERT INTO agent_subscriptions(id,uuid,name,valid_from) "
                    "VALUES(1,'sub','subscription','2026-01-01')")
                conn.execute(
                    "INSERT INTO agent_subscription_instances "
                    "(id,uuid,subscription_id,valid_from) "
                    "VALUES(1,'inst',1,'2026-01-01')")
                conn.execute(
                    "INSERT INTO agent_subscription_period_charges "
                    "(id,instance_id,subscription_id,period_start,period_end,"
                    "recurring_charge,currency) "
                    "VALUES(1,1,1,'2026-01-01','2026-02-01',1,'CNY')")
                conn.execute(
                    "INSERT INTO agent_subscription_charge_allocations "
                    "(period_charge_id,software_id) VALUES(1,1)")
                conn.commit()
            snapshot_config(proxy)

            def capture(config, path):
                del config
                with sqlite3.connect(path) as uploaded:
                    captured["period_charges"] = uploaded.execute(
                        "SELECT count(*) FROM agent_subscription_period_charges"
                    ).fetchone()[0]
                    captured["allocations"] = uploaded.execute(
                        "SELECT count(*) FROM agent_subscription_charge_allocations"
                    ).fetchone()[0]
                    captured["foreign_key_check"] = uploaded.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                return RemoteArtifact("token-board_config_20260824_120000.db")

            with patch(
                    "app.services.sync.config_sync.publish_config_artifact",
                    side_effect=capture):
                result = sync_config_upload(proxy)

            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(captured["period_charges"], 0)
            self.assertEqual(captured["allocations"], 0)
            self.assertEqual(captured["foreign_key_check"], [])
            with sqlite3.connect(proxy) as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM agent_subscription_period_charges"
                ).fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM agent_subscription_charge_allocations"
                ).fetchone()[0], 1)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_upload_failure_rolls_back_to_snapshot(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            self._webdav_mocks()
            from app.services.sync.snapshot import snapshot_config
            snapshot_config(proxy)
            with sqlite3.connect(proxy) as conn:
                conn.execute("INSERT INTO accounts(id,uuid,name) VALUES(1,'a','base')")
                conn.commit()
            snapshot_config(proxy)
            with sqlite3.connect(proxy) as conn:
                conn.execute("UPDATE accounts SET name='changed' WHERE id=1")
                conn.commit()
            with patch("app.services.sync.config_sync.publish_config_artifact",
                       side_effect=WebDAVError("put failed")):
                result = sync_config_upload(proxy)
            self.assertEqual(result["status"], "rolled_back", result)
            with sqlite3.connect(proxy) as conn:
                self.assertEqual(conn.execute(
                    "SELECT name FROM accounts WHERE id=1").fetchone()[0], "base")
        finally:
            patch.stopall()
            shutil.rmtree(temp, ignore_errors=True)

    def test_config_snapshot_removes_generated_charge_children_first(self) -> None:
        temp, proxy, _ = self._repo_layout()
        try:
            from app.services.sync.snapshot import snapshot_config
            with sqlite3.connect(proxy) as conn:
                conn.execute("INSERT INTO accounts(id,uuid,name) VALUES(1,'acct','agent')")
                conn.execute(
                    "INSERT INTO agent_subscriptions(id,uuid,name,valid_from) "
                    "VALUES(1,'sub','subscription','2026-01-01')")
                conn.execute(
                    "INSERT INTO agent_subscription_instances "
                    "(id,uuid,subscription_id,valid_from) VALUES(1,'inst',1,'2026-01-01')")
                conn.execute(
                    "INSERT INTO agent_subscription_period_charges "
                    "(id,instance_id,subscription_id,period_start,period_end,"
                    "recurring_charge,currency) VALUES(1,1,1,'2026-01-01','2026-02-01',1,'CNY')")
                conn.execute(
                    "INSERT INTO agent_subscription_charge_allocations "
                    "(period_charge_id,software_id) VALUES(1,1)")
                conn.commit()
            snapshot_config(proxy)
            with sqlite3.connect(proxy) as conn:
                self.assertEqual(conn.execute(
                    "PRAGMA foreign_key_check").fetchall(), [])
            with sqlite3.connect(str(Path(temp) / "data/token-board_config_snapshot.db")) as conn:
                self.assertEqual(conn.execute(
                    "PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM agent_subscription_charge_allocations"
                ).fetchone()[0], 0)
        finally:
            shutil.rmtree(temp, ignore_errors=True)

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
        patch("app.services.sync.config_sync.publish_config_artifact",
              return_value=RemoteArtifact(
                  "token-board_config_20260824_120000.db",
                  etag='"etag"')).start()
        for module in ("app.services.sync.dashboard_sync",):
            patch(module + ".latest_artifact", return_value=None).start()
            patch(module + ".download_artifact", return_value=False).start()
            patch(module + ".publish_versioned_artifact",
                  return_value=RemoteArtifact(
                      "dashboard_sync_20260824_120000.db",
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

            def capture(config, path):
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

            with patch("app.services.sync.config_sync.publish_config_artifact",
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
