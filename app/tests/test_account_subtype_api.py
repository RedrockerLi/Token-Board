from __future__ import annotations

import json
import socket as socket_module
from unittest.mock import patch

from app import create_app
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class AccountSubtypeApiTest(AppDatabaseTestCase):
    """Plan sub-types, per-key labels and the usage-refresh wake-up."""

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()

    def _account(self, account_id: int) -> dict:
        accounts = self.client.get("/api/proxy/accounts").get_json()
        return next(item for item in accounts if item["id"] == account_id)

    def _upstream_subtype(self, account_id: int):
        with sqlite_connection(self.proxy_path) as conn:
            return conn.execute(
                "SELECT subtype FROM upstreams WHERE account_id=?",
                (account_id,)).fetchone()[0]

    def _create_go_account(self, name: str, key: str | None = None) -> int:
        payload = {"name": name, "account_type": "plan",
                   "subtype": "opencode-go"}
        if key:
            payload["upstream_keys"] = [key]
        created = self.client.post("/api/proxy/accounts", json=payload)
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        return created.get_json()["id"]

    def _write_usage(self, account_id: int, payload: dict) -> None:
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE upstream_credentials SET usage_json=? WHERE upstream_id="
                "(SELECT id FROM upstreams WHERE account_id=? LIMIT 1)",
                (json.dumps(payload), account_id))

    def test_plan_subtype_is_validated_stored_and_echoed(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "go-plan",
            "account_type": "plan",
            "subtype": "opencode-go",
            "base_url": "https://opencode.ai/zen/go",
            "upstream_keys": ["sk-go-subtype"],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        account_id = created.get_json()["id"]

        account = self._account(account_id)
        self.assertEqual(account["account_type"], "plan")
        self.assertEqual(account["subtype"], "opencode-go")
        self.assertEqual(self._upstream_subtype(account_id), "opencode-go")

        # A sub-type the plan spec does not declare is rejected.
        unknown = self.client.post("/api/proxy/accounts", json={
            "name": "go-unknown", "account_type": "plan",
            "subtype": "opencode-max",
        })
        self.assertEqual(unknown.status_code, 400)
        # The api type declares no sub-types, so only empty/null is accepted.
        mismatch = self.client.post("/api/proxy/accounts", json={
            "name": "go-api", "account_type": "api",
            "subtype": "opencode-go",
        })
        self.assertEqual(mismatch.status_code, 400)
        # Plain accounts keep the null sub-type in the payload.
        plain = self.client.post("/api/proxy/accounts", json={
            "name": "plain-api", "account_type": "api",
        })
        self.assertEqual(plain.status_code, 201)
        self.assertIsNone(self._account(plain.get_json()["id"])["subtype"])

    def test_type_change_clears_subtype_and_api_rejects_it(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "go-to-api", "account_type": "plan",
            "subtype": "opencode-go", "monthly_price": 10,
        })
        self.assertEqual(created.status_code, 201)
        account_id = created.get_json()["id"]

        renamed = self.client.put(f"/api/proxy/accounts/{account_id}",
                                  json={"name": "go-renamed"})
        self.assertEqual(renamed.status_code, 200,
                         renamed.get_data(as_text=True))
        self.assertEqual(self._account(account_id)["subtype"], "opencode-go")

        rejected = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "account_type": "api", "subtype": "opencode-go"})
        self.assertEqual(rejected.status_code, 400)

        switched = self.client.put(f"/api/proxy/accounts/{account_id}",
                                   json={"account_type": "api"})
        self.assertEqual(switched.status_code, 200,
                         switched.get_data(as_text=True))
        account = self._account(account_id)
        self.assertEqual(account["account_type"], "api")
        self.assertIsNone(account["subtype"])
        self.assertIsNone(self._upstream_subtype(account_id))

        # Switching back does not resurrect the cleared sub-type.
        back = self.client.put(f"/api/proxy/accounts/{account_id}",
                               json={"account_type": "plan"})
        self.assertEqual(back.status_code, 200)
        self.assertIsNone(self._account(account_id)["subtype"])

    def test_key_labels_round_trip_through_create_and_update(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "labeled-keys", "account_type": "api",
            "base_url": "http://labeled.test",
            "upstream_keys": ["sk-alpha-111111", "sk-beta-222222"],
            "new_labels": ["  alpha key  ", "   "],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        account_id = created.get_json()["id"]
        keys = self._account(account_id)["keys"]
        self.assertEqual([key["label"] for key in keys],
                         ["alpha key", None])

        # A shorter label list leaves the unlabelled key at NULL.
        short = self.client.post("/api/proxy/accounts", json={
            "name": "short-labels", "account_type": "api",
            "upstream_keys": ["sk-one-111111", "sk-two-222222"],
            "new_labels": ["only-first"],
        })
        self.assertEqual(short.status_code, 201)
        self.assertEqual(
            [key["label"] for key in self._account(short.get_json()["id"])["keys"]],
            ["only-first", None])

        keep_id = keys[0]["id"]
        updated = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "keys_edited": True,
            "keep_key_ids": [keep_id],
            "keep_labels": {str(keep_id): "  renamed  "},
            "upstream_keys": ["sk-gamma-333333"],
            "new_labels": ["gamma key"],
        })
        self.assertEqual(updated.status_code, 200,
                         updated.get_data(as_text=True))
        by_id = {key["id"]: key for key in self._account(account_id)["keys"]}
        self.assertEqual(by_id[keep_id]["label"], "renamed")
        self.assertEqual(len(by_id), 2)
        gamma = next(key for key in by_id.values() if key["id"] != keep_id)
        self.assertEqual(gamma["label"], "gamma key")

        # An empty label clears the stored value back to NULL.
        cleared = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "keys_edited": True,
            "keep_key_ids": [keep_id],
            "keep_labels": {str(keep_id): ""},
            "upstream_keys": [],
        })
        self.assertEqual(cleared.status_code, 200)
        by_id = {key["id"]: key for key in self._account(account_id)["keys"]}
        self.assertIsNone(by_id[keep_id]["label"])

        # Labels stay aligned when the request carries blank key slots, and
        # non-string labels never become literal "0"/"False" names.
        aligned = self.client.post("/api/proxy/accounts", json={
            "name": "aligned-labels", "account_type": "api",
            "upstream_keys": ["", "  ", "sk-aligned-111111"],
            "new_labels": ["", 0, "aligned key"],
        })
        self.assertEqual(aligned.status_code, 201)
        aligned_keys = self._account(aligned.get_json()["id"])["keys"]
        self.assertEqual([key["label"] for key in aligned_keys], ["aligned key"])
        numeric = self.client.post("/api/proxy/accounts", json={
            "name": "numeric-label", "account_type": "api",
            "upstream_keys": ["sk-numeric-111111"],
            "new_labels": [False],
        })
        self.assertEqual(numeric.status_code, 201)
        self.assertIsNone(
            self._account(numeric.get_json()["id"])["keys"][0]["label"])

    def test_key_labels_survive_edits_that_do_not_manage_them(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "keep-labels", "account_type": "api",
            "upstream_keys": ["sk-keep-111111", "sk-keep-222222"],
            "new_labels": ["first", "second"],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        account_id = created.get_json()["id"]
        keep_ids = [key["id"] for key in self._account(account_id)["keys"]]

        # A key edit without a label map (legacy client shape) must not wipe
        # the stored names.
        edited = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "keys_edited": True,
            "keep_key_ids": keep_ids,
            "upstream_keys": [],
        })
        self.assertEqual(edited.status_code, 200,
                         edited.get_data(as_text=True))
        self.assertEqual([key["label"] for key in self._account(account_id)["keys"]],
                         ["first", "second"])

    def test_key_edit_preserves_cloud_mirror_credentials(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "mirror-keep", "account_type": "api",
            "upstream_keys": ["sk-local-111111"],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        account_id = created.get_json()["id"]
        keep_ids = [key["id"] for key in self._account(account_id)["keys"]]

        # A cloud-mirror credential: masked metadata without local plaintext,
        # exactly what a config pull leaves behind for cloud-only keys.
        with sqlite_connection(self.proxy_path) as conn:
            upstream_id = conn.execute(
                "SELECT id FROM upstreams WHERE account_id=?",
                (account_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO upstream_credentials"
                "(uuid,runtime_id,upstream_id,position,key_masked,enabled) "
                "VALUES('mirror-uuid-0001',999,?,9,'sk-clo…oud',1)",
                (upstream_id,))

        edited = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "keys_edited": True,
            "keep_key_ids": keep_ids,
            "keep_labels": {str(key_id): "保留" for key_id in keep_ids},
            "upstream_keys": [],
        })
        self.assertEqual(edited.status_code, 200,
                         edited.get_data(as_text=True))
        with sqlite_connection(self.proxy_path) as conn:
            mirror = conn.execute(
                "SELECT 1 FROM upstream_credentials WHERE uuid='mirror-uuid-0001'"
            ).fetchone()
        self.assertIsNotNone(mirror, "本地密钥编辑不得删除云端镜像行")

    def test_update_without_account_type_keeps_subtype_during_cancellation(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "cancelling-go", "account_type": "plan",
            "subtype": "opencode-go", "monthly_price": 10,
            "upstream_keys": ["sk-cancel-111111"],
        })
        self.assertEqual(created.status_code, 201)
        account_id = created.get_json()["id"]
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_contracts SET ends_at='2099-01-01T00:00:00Z' "
                "WHERE account_id=?", (account_id,))

        # The live-contract join no longer sees the ending contract, but an
        # update that does not touch the type must keep the stored sub-type.
        renamed = self.client.put(f"/api/proxy/accounts/{account_id}",
                                  json={"name": "cancelling-go-renamed"})
        self.assertEqual(renamed.status_code, 200,
                         renamed.get_data(as_text=True))
        self.assertEqual(self._upstream_subtype(account_id), "opencode-go")

    def test_key_usage_snapshot_passes_through_defensively(self) -> None:
        created = self.client.post("/api/proxy/accounts", json={
            "name": "usage-keys", "account_type": "plan",
            "subtype": "opencode-go",
            "upstream_keys": ["sk-usage-111111"],
        })
        self.assertEqual(created.status_code, 201)
        account_id = created.get_json()["id"]
        runtime_id = self._account(account_id)["keys"][0]["id"]
        self.assertIsNone(self._account(account_id)["keys"][0]["usage"])

        snapshot = {
            "checked_at": "2026-10-12T04:00:00Z",
            "error": None,
            "windows": {
                "5h": {"percent": 42, "resets_at": "2026-10-12T06:00:00Z"},
                # A 0% window carries its reset too: percent counts usage, it
                # does not report whether the window is live.
                "week": {"percent": 0, "resets_at": "2026-10-19T00:00:00Z"},
                "month": {"percent": 7, "resets_at": None},
            },
            "monthly_period": {"start": "2026-10-01", "end": None},
        }
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE upstream_credentials SET usage_json=? WHERE runtime_id=?",
                (json.dumps(snapshot), runtime_id),
            )
        self.assertEqual(self._account(account_id)["keys"][0]["usage"], snapshot)

        # Malformed, non-object or pathologically nested snapshots read as
        # null instead of failing the whole accounts payload.
        for raw in ("{broken", json.dumps([1, 2]), "[" * 10000):
            with sqlite_connection(self.proxy_path) as conn:
                conn.execute(
                    "UPDATE upstream_credentials SET usage_json=? "
                    "WHERE runtime_id=?", (raw, runtime_id),
                )
            self.assertIsNone(self._account(account_id)["keys"][0]["usage"])

    def test_usage_refresh_wakes_poller_only_for_usage_tracked_accounts(self) -> None:
        payload = {
            "name": "go-poll", "account_type": "plan",
            "subtype": "opencode-go",
            "upstream_keys": ["sk-poll-111111"],
        }
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            created = self.client.post("/api/proxy/accounts", json=payload)
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        factory.assert_called_once_with(socket_module.AF_UNIX,
                                        socket_module.SOCK_DGRAM)
        client = factory.return_value.__enter__.return_value
        client.settimeout.assert_called_once_with(0.2)
        client.sendto.assert_called_once_with(
            b"USAGE_REFRESH", self.app.config["MAINTENANCE_SOCKET"])

        account_id = created.get_json()["id"]
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            updated = self.client.put(f"/api/proxy/accounts/{account_id}",
                                      json={"max_concurrency": 2})
        self.assertEqual(updated.status_code, 200)
        factory.return_value.__enter__.return_value.sendto.assert_called_once_with(
            b"USAGE_REFRESH", self.app.config["MAINTENANCE_SOCKET"])

        # Accounts without a usage query path never wake the poller.
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            plain = self.client.post("/api/proxy/accounts", json={
                "name": "plain-poll", "account_type": "api"})
            self.assertEqual(plain.status_code, 201)
        factory.assert_not_called()

        # A dead maintenance socket must not fail the write.
        with patch("app.services.runtime_tasks.socket.socket",
                   side_effect=OSError("no socket")):
            offline = self.client.post("/api/proxy/accounts", json=dict(
                payload, name="go-poll-offline"))
        self.assertEqual(offline.status_code, 201,
                         offline.get_data(as_text=True))
        self.assertEqual(
            self._account(offline.get_json()["id"])["subtype"], "opencode-go")

    def test_usage_refresh_returns_the_fresh_account_row(self) -> None:
        account_id = self._create_go_account("go-click", "sk-click-111111")
        snapshot = {"checked_at": "2026-10-14T06:55:00Z", "error": None,
                    "windows": {}}
        # The wake's sendto commits a new observation inline: the wait loop
        # compares before its first sleep, so the call returns with no delay.
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            factory.return_value.__enter__.return_value.sendto.side_effect = (
                lambda *_: self._write_usage(account_id, snapshot))
            response = self.client.post(
                f"/api/proxy/accounts/{account_id}/usage-refresh")
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["account"]["keys"][0]["usage"], snapshot)
        client = factory.return_value.__enter__.return_value
        client.settimeout.assert_called_once_with(0.2)
        client.sendto.assert_called_once_with(
            b"USAGE_REFRESH", self.app.config["MAINTENANCE_SOCKET"])

    def test_usage_refresh_reports_pending_without_a_new_snapshot(self) -> None:
        account_id = self._create_go_account("go-pending", "sk-pending-111111")
        with patch("app.services.runtime_tasks.socket.socket") as factory, \
                patch("app.routes.proxy.accounts.USAGE_REFRESH_WAIT_SECONDS", 0.05), \
                patch("app.routes.proxy.accounts.USAGE_REFRESH_POLL_SECONDS", 0.01):
            response = self.client.post(
                f"/api/proxy/accounts/{account_id}/usage-refresh")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"status": "pending"})
        # The wake was still delivered; only the observation has not landed.
        factory.return_value.__enter__.return_value.sendto.assert_called_once_with(
            b"USAGE_REFRESH", self.app.config["MAINTENANCE_SOCKET"])

    def test_usage_refresh_validates_the_account(self) -> None:
        missing = self.client.post("/api/proxy/accounts/999/usage-refresh")
        self.assertEqual(missing.status_code, 404)

        plain = self.client.post("/api/proxy/accounts", json={
            "name": "plain-refresh", "account_type": "api"})
        self.assertEqual(plain.status_code, 201)
        unsupported = self.client.post(
            f"/api/proxy/accounts/{plain.get_json()['id']}/usage-refresh")
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.get_json()["error"],
                         "该账户类型不支持用量查询")

        keyless = self._create_go_account("go-keyless")
        no_keys = self.client.post(
            f"/api/proxy/accounts/{keyless}/usage-refresh")
        self.assertEqual(no_keys.status_code, 400)
        self.assertEqual(no_keys.get_json()["error"],
                         "该账户没有可查询用量的本机密钥")

    def test_usage_refresh_unavailable_without_the_maintenance_socket(self) -> None:
        account_id = self._create_go_account("go-offline", "sk-offline-111111")
        with patch("app.services.runtime_tasks.socket.socket",
                   side_effect=OSError("no socket")):
            response = self.client.post(
                f"/api/proxy/accounts/{account_id}/usage-refresh")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "unavailable")

    def test_usage_refresh_watches_only_the_requested_account(self) -> None:
        account_a = self._create_go_account("go-watch-a", "sk-watch-aaaa")
        account_b = self._create_go_account("go-watch-b", "sk-watch-bbbb")
        snapshot = {"checked_at": "2026-10-14T06:56:00Z", "error": None,
                    "windows": {}}
        # The wake refreshes the whole poller cycle, but a waiter only resolves
        # on its own account's snapshot: B committing must not resolve A.
        with patch("app.services.runtime_tasks.socket.socket") as factory, \
                patch("app.routes.proxy.accounts.USAGE_REFRESH_WAIT_SECONDS", 0.05), \
                patch("app.routes.proxy.accounts.USAGE_REFRESH_POLL_SECONDS", 0.01):
            factory.return_value.__enter__.return_value.sendto.side_effect = (
                lambda *_: self._write_usage(account_b, snapshot))
            untouched = self.client.post(
                f"/api/proxy/accounts/{account_a}/usage-refresh")
        self.assertEqual(untouched.status_code, 202,
                         untouched.get_data(as_text=True))

        fresh = {**snapshot, "checked_at": "2026-10-14T06:57:00Z"}
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            factory.return_value.__enter__.return_value.sendto.side_effect = (
                lambda *_: self._write_usage(account_b, fresh))
            resolved = self.client.post(
                f"/api/proxy/accounts/{account_b}/usage-refresh")
        self.assertEqual(resolved.status_code, 200,
                         resolved.get_data(as_text=True))
        self.assertEqual(
            resolved.get_json()["account"]["keys"][0]["usage"], fresh)

    def test_usage_refresh_counts_a_failed_observation_as_fresh(self) -> None:
        account_id = self._create_go_account("go-failed", "sk-failed-111111")
        failure = {"checked_at": None, "error": "HTTP 500",
                   "error_at": "2026-10-14T06:58:00Z", "windows": {}}
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            factory.return_value.__enter__.return_value.sendto.side_effect = (
                lambda *_: self._write_usage(account_id, failure))
            response = self.client.post(
                f"/api/proxy/accounts/{account_id}/usage-refresh")
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True))
        self.assertEqual(response.get_json()["account"]["keys"][0]["usage"],
                         failure)
