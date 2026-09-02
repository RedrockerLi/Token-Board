from __future__ import annotations

import sqlite3
import unittest

from app import create_app
from app.tests.support import AppDatabaseTestCase


class DashboardUserHttpTest(AppDatabaseTestCase):
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
            conn.commit()
        self.app = create_app(
            str(self.proxy_path), testing=True,
            schema_dir=str(self.root / "schema"),
            start_background_tasks=False,
        )
        self.client = self.app.test_client()

    def test_batch_delete_returns_result_and_refreshes_store(self) -> None:
        response = self.client.delete(
            "/api/proxy/dashboard/users",
            json={"names": ["remove-me"]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["deleted_names"], ["remove-me"])
        self.assertEqual(payload["not_found_names"], [])
        self.assertFalse(payload["uploaded"])
        self.assertEqual(self.app.config["DATA_STORE"].api_key_names, ["keep-me"])

    def test_invalid_and_all_missing_requests_have_explicit_statuses(self) -> None:
        invalid = self.client.delete(
            "/api/proxy/dashboard/users", json={"names": "remove-me"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["status"], "invalid")

        missing = self.client.delete(
            "/api/proxy/dashboard/users", json={"names": ["missing"]})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["status"], "not_found")

    def test_legacy_upload_endpoint_is_removed(self) -> None:
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertNotIn("/api/proxy/dashboard/users/upload", routes)


if __name__ == "__main__":
    unittest.main()
