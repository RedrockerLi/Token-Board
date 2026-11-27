from __future__ import annotations

from app import create_app
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class AccountDuplicateTest(AppDatabaseTestCase):
    """复制账户：新账户拿到同一份配置（含本机明文密钥与模型目录），源账户不变。

    复制是一个独立账户：自己的计费合同、上游、路由集与密钥行；源的计费日期、
    用量快照和到期删除安排都不随配置走。
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()
        self.db = self.proxy_database()

    def _create(self, **overrides) -> int:
        payload = {
            "name": "copy-source",
            "account_type": "api",
            "base_url": "http://upstream.test/v1",
            "api_format": "openai",
            "upstream_keys": ["sk-copy-0001"],
        }
        payload.update(overrides)
        created = self.client.post("/api/proxy/accounts", json=payload)
        self.assertEqual(created.status_code, 201,
                         created.get_data(as_text=True))
        return created.get_json()["id"]

    def _accounts(self) -> list[dict]:
        response = self.client.get("/api/proxy/accounts")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _duplicate(self, account_id: int):
        return self.client.post(f"/api/proxy/accounts/{account_id}/duplicate")

    def test_duplicate_copies_routing_fields_keys_and_catalog(self) -> None:
        source_id = self._create(
            api_format="anthropic", endpoint_path="/v1/messages",
            auth_header="x-api-key", max_concurrency=3,
            upstream_keys=["sk-copy-0001", "sk-copy-0002"],
            new_labels=["主力", "备用"])
        self.db.update_account_models(source_id, ["m-a", "m-b"])
        source = next(a for a in self._accounts() if a["id"] == source_id)

        response = self._duplicate(source_id)
        self.assertEqual(response.status_code, 201,
                         response.get_data(as_text=True))
        created = response.get_json()
        self.assertNotEqual(created["id"], source_id)
        self.assertEqual(created["name"], "copy-source 副本")
        self.assertEqual(created["keys_copied"], 2)

        copy = next(a for a in self._accounts() if a["id"] == created["id"])
        for field in ("account_type", "subtype", "base_url", "api_format",
                      "endpoint_path", "auth_header", "max_concurrency",
                      "currency"):
            self.assertEqual(copy[field], source[field], field)
        self.assertEqual([(key["masked"], key["label"]) for key in copy["keys"]],
                         [(key["masked"], key["label"]) for key in source["keys"]])
        self.assertEqual(copy["key_count"], source["key_count"])
        self.assertEqual(self.db.get_account_models(created["id"]),
                         ["m-a", "m-b"])

        # 源账户的配置与密钥没有被改动。
        after = next(a for a in self._accounts() if a["id"] == source_id)
        self.assertEqual(after["name"], "copy-source")
        self.assertEqual(after["key_count"], 2)
        self.assertEqual([(key["masked"], key["label"]) for key in after["keys"]],
                         [(key["masked"], key["label"]) for key in source["keys"]])

    def test_duplicate_numbers_repeated_copies(self) -> None:
        source_id = self._create()
        first = self._duplicate(source_id).get_json()
        second = self._duplicate(source_id).get_json()
        self.assertEqual(first["name"], "copy-source 副本")
        self.assertEqual(second["name"], "copy-source 副本 2")

    def test_duplicate_plan_account_keeps_subtype_price_and_contract(self) -> None:
        source_id = self._create(
            name="plan-source", account_type="plan", subtype="opencode-go",
            base_url="https://opencode.ai/zen/go", api_format="anthropic",
            monthly_price=10, currency="USD", upstream_keys=["sk-plan-0001"])
        source = next(a for a in self._accounts() if a["id"] == source_id)

        created = self._duplicate(source_id).get_json()
        copy = next(a for a in self._accounts() if a["id"] == created["id"])
        self.assertEqual(copy["account_type"], "plan")
        self.assertEqual(copy["subtype"], "opencode-go")
        self.assertEqual(copy["monthly_price"], source["monthly_price"])
        self.assertEqual(copy["currency"], "USD")
        with sqlite_connection(self.proxy_path) as conn:
            contract = conn.execute(
                "SELECT charge_type,billing_scope,currency FROM billing_contracts "
                "WHERE account_id=?", (created["id"],)).fetchone()
            self.assertEqual(tuple(contract), ("recurring", "credential", "USD"))
            keys = conn.execute(
                "SELECT count(*) FROM upstream_credentials c JOIN upstreams u "
                "ON u.id=c.upstream_id WHERE u.account_id=?",
                (created["id"],)).fetchone()[0]
        self.assertEqual(keys, 1)

    def test_duplicate_missing_or_ended_account_is_rejected(self) -> None:
        missing = self._duplicate(999999)
        self.assertEqual(missing.status_code, 404,
                         missing.get_data(as_text=True))

        source_id = self._create()
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "UPDATE billing_contracts SET ends_at=? WHERE account_id=?",
                ("2000-01-01T00:00:00Z", source_id))
        ended = self._duplicate(source_id)
        self.assertEqual(ended.status_code, 409,
                         ended.get_data(as_text=True))
        self.assertEqual(
            [a["id"] for a in self._accounts()
             if a["name"].startswith("copy-source 副本")],
            [])

    def test_duplicate_account_without_local_keys_copies_none(self) -> None:
        source_id = self._create(upstream_keys=[])
        response = self._duplicate(source_id)
        self.assertEqual(response.status_code, 201,
                         response.get_data(as_text=True))
        created = response.get_json()
        self.assertEqual(created["keys_copied"], 0)
        copy = next(a for a in self._accounts() if a["id"] == created["id"])
        self.assertEqual(copy["keys"], [])
        self.assertEqual(copy["key_count"], 0)

    def test_duplicate_rejects_aggregate_route_set_id(self) -> None:
        source_id = self._create()
        aggregate = self.client.post("/api/proxy/aggregates", json={
            "name": "copy-aggregate",
            "entries": [{"pattern": "m-1", "account_id": source_id,
                         "upstream_model": "m-1"}],
        })
        self.assertEqual(aggregate.status_code, 201,
                         aggregate.get_data(as_text=True))
        rejected = self._duplicate(aggregate.get_json()["id"])
        self.assertEqual(rejected.status_code, 404,
                         rejected.get_data(as_text=True))

    def test_duplicate_keeps_best_effort_catalog_failure_observable(self) -> None:
        """03-F-17: 模型目录复制失败仍是 best-effort，但必须留下 warning。"""
        source_id = self._create()
        self.db.update_account_models(source_id, ["m-a", "m-b"])
        # Only the model-catalog copy fails: the table is gone, exactly like a
        # partially migrated installation.  Patching the routing helper would
        # also break account serialization, which is a different failure.
        with sqlite_connection(self.proxy_path) as connection:
            connection.execute("DROP TABLE upstream_model_catalog")
        with self.assertLogs("app.db.proxy.accounts_write",
                             level="WARNING") as captured:
            response = self._duplicate(source_id)
        created_id = response.get_json()["id"]
        self.assertEqual(response.status_code, 201,
                         response.get_data(as_text=True))
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any("model catalog copy failed" in message
                            and str(created_id) in message
                            for message in messages),
                        messages)
        created = response.get_json()
        # The best-effort copy still succeeds and keeps its declared shape.
        self.assertEqual(set(created), {"id", "name", "keys_copied"})
        self.assertEqual(created["name"], "copy-source 副本")
