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
                "INSERT INTO users(id,name,actual_cost_micro_cny) VALUES(?,?,?)",
                [(7, "remove-me", 0), (8, "keep-me", 0)],
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
        self.app = create_app(
            str(self.proxy_path), testing=True,
            schema_dir=str(self.root / "schema"),
            start_background_tasks=False,
        )
        self.client = self.app.test_client()

    def test_batch_delete_returns_result_and_refreshes_store(self) -> None:
        response = self.client.delete(
            "/api/proxy/dashboard/users",
            json={"user_ids": [7]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["archived_user_ids"], [7])
        self.assertEqual(payload["not_found_user_ids"], [])
        self.assertFalse(payload["uploaded"])
        self.assertEqual(self.app.config["DATA_STORE"].api_key_names,
                         ["keep-me", "归档"])

    def test_invalid_and_all_missing_requests_have_explicit_statuses(self) -> None:
        invalid = self.client.delete(
            "/api/proxy/dashboard/users", json={"user_ids": "7"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["status"], "invalid")

        missing = self.client.delete(
            "/api/proxy/dashboard/users", json={"user_ids": [999]})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["status"], "not_found")

    def test_legacy_upload_endpoint_is_removed(self) -> None:
        routes = {rule.rule for rule in self.app.url_map.iter_rules()}
        self.assertNotIn("/api/proxy/dashboard/users/upload", routes)


if __name__ == "__main__":
    unittest.main()
