from __future__ import annotations

from app import create_app
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class EndingAccountEditTest(AppDatabaseTestCase):
    """Editing a plan account whose deletion is deferred to the period end.

    `end_of_period` keeps the contract as the account's live one until its
    `ends_at` boundary, so an edit must find that contract instead of treating
    the account as contract-less.  Retyping is the one structural change the
    window refuses: a metered contract would stop being a billing unit and
    would drop the "cannot add keys" guard.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()

    def _create_plan(self, name: str = "go-plan") -> int:
        created = self.client.post("/api/proxy/accounts", json={
            "name": name,
            "account_type": "plan",
            "subtype": "opencode-go",
            "monthly_price": 10,
            "base_url": "https://opencode.ai/zen/go",
            "api_format": "anthropic",
            "upstream_keys": ["sk-ending-0001"],
        })
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        return created.get_json()["id"]

    def _ui_payload(self, name: str, account_type: str = "plan") -> dict:
        """The exact payload `saveAccount` submits for a routable account."""
        return {
            "name": name,
            "account_type": account_type,
            "subtype": "opencode-go" if account_type == "plan" else None,
            "monthly_price": 10 if account_type == "plan" else 0,
            "currency": "CNY",
            "base_url": "https://opencode.ai/zen/go",
            "api_format": "anthropic",
            "endpoint_path": "",
            "auth_header": "auto",
            "max_concurrency": None,
        }

    def _schedule_deletion(self, account_id: int) -> str:
        deleted = self.client.delete(f"/api/proxy/accounts/{account_id}")
        self.assertEqual(deleted.status_code, 200,
                         deleted.get_data(as_text=True))
        body = deleted.get_json()
        self.assertTrue(body["deferred"], body)
        return body["effective_ends_at"]

    def _contract(self, account_id: int) -> dict:
        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT charge_type,billing_scope,currency,ends_at "
                "FROM billing_contracts WHERE account_id=?", (account_id,)
            ).fetchone()
        return {"charge_type": row[0], "billing_scope": row[1],
                "currency": row[2], "ends_at": row[3]}

    def _rate_event_count(self) -> int:
        with sqlite_connection(self.proxy_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM billing_rate_events").fetchone()[0]

    def _upstream_subtype(self, account_id: int):
        with sqlite_connection(self.proxy_path) as conn:
            return conn.execute(
                "SELECT subtype FROM upstreams WHERE account_id=?",
                (account_id,)).fetchone()[0]

    def _account(self, account_id: int) -> dict:
        accounts = self.client.get("/api/proxy/accounts").get_json()
        return next(item for item in accounts if item["id"] == account_id)

    def test_full_ui_payload_saves_after_deferred_deletion(self) -> None:
        account_id = self._create_plan()
        ends_at = self._schedule_deletion(account_id)
        events_before = self._rate_event_count()

        saved = self.client.put(f"/api/proxy/accounts/{account_id}",
                                json=self._ui_payload("go-plan-renamed"))
        self.assertEqual(saved.status_code, 200,
                         saved.get_data(as_text=True))

        # The rename lands, the contract keeps its boundary and stays the
        # subscription it was, and no billing fact is invented.
        self.assertEqual(self._account(account_id)["name"], "go-plan-renamed")
        contract = self._contract(account_id)
        self.assertEqual(contract["charge_type"], "recurring")
        self.assertEqual(contract["ends_at"], ends_at)
        self.assertEqual(self._rate_event_count(), events_before)
        self.assertEqual(self._upstream_subtype(account_id), "opencode-go")

    def test_retype_is_rejected_while_deletion_is_pending(self) -> None:
        account_id = self._create_plan()
        ends_at = self._schedule_deletion(account_id)

        rejected = self.client.put(f"/api/proxy/accounts/{account_id}",
                                   json=self._ui_payload("go-plan",
                                                         account_type="api"))
        self.assertEqual(rejected.status_code, 409,
                         rejected.get_data(as_text=True))
        # Nothing was half-applied: the contract is still the plan contract and
        # the sub-type was not dropped on the way to a refused retype.
        contract = self._contract(account_id)
        self.assertEqual(contract["charge_type"], "recurring")
        self.assertEqual(contract["ends_at"], ends_at)
        self.assertEqual(self._upstream_subtype(account_id), "opencode-go")
        self.assertEqual(self._account(account_id)["account_type"], "plan")

    def test_edit_is_rejected_once_the_contract_has_ended(self) -> None:
        account_id = self._create_plan()
        self._schedule_deletion(account_id)
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_contracts SET ends_at='2020-01-01T00:00:00Z' "
                "WHERE account_id=?", (account_id,))

        rejected = self.client.put(f"/api/proxy/accounts/{account_id}",
                                   json=self._ui_payload("too-late"))
        self.assertEqual(rejected.status_code, 409,
                         rejected.get_data(as_text=True))
        self.assertEqual(self._account(account_id)["name"], "go-plan")
        # Only the rate event written at creation exists; the refused save
        # must not append one for a contract that is already past its end.
        self.assertEqual(self._rate_event_count(), 1)

    def test_key_edits_are_rejected_while_deletion_is_pending(self) -> None:
        account_id = self._create_plan()
        self._schedule_deletion(account_id)

        for label, payload in (
            ("add", {"upstream_keys": ["sk-ending-0002"],
                     "keep_key_ids": []}),
            ("remove", {"upstream_keys": [], "keep_key_ids": []}),
        ):
            with self.subTest(label=label):
                rejected = self.client.put(
                    f"/api/proxy/accounts/{account_id}",
                    json={**self._ui_payload("go-plan"),
                          "keys_edited": True,
                          "new_labels": [""],
                          "keep_labels": {},
                          "keep_valid_froms": {},
                          **payload})
                self.assertEqual(rejected.status_code, 409,
                                 rejected.get_data(as_text=True))
                # The refusal names what it refuses: every key edit, not just
                # an addition, because the guard runs before the key diff.
                self.assertIn("编辑密钥", rejected.get_json()["error"])
        self.assertEqual(self._upstream_subtype(account_id), "opencode-go")
