from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from unittest.mock import patch

from app import create_app
from app.db.dashboard_db import DashboardDatabase
from app.db.proxy_db import ProxyDatabase
from app.core.time import parse_runtime_timestamp

from app.tests.support import AppDatabaseTestCase


class AppContractTest(AppDatabaseTestCase):
    def test_billing_config_uses_fixed_next_period_pricing(self) -> None:
        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        client = app.test_client()
        initial = client.get("/api/proxy/billing-config")
        self.assertEqual(initial.status_code, 200)
        self.assertNotIn("price_change_effective", initial.get_json())

        saved = client.put("/api/proxy/billing-config", json={
            "cancellation_mode": "immediate",
        })
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("price_change_effective", saved.get_json())

        rejected = client.put("/api/proxy/billing-config", json={
            "price_change_effective": "current_period",
            "cancellation_mode": "immediate",
        })
        self.assertEqual(rejected.status_code, 400)

    def test_public_facades_and_v1_http_contract(self) -> None:
        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        client = app.test_client()

        account_types = client.get("/api/proxy/account-types")
        self.assertEqual(account_types.status_code, 200)
        self.assertEqual(set(account_types.get_json()), {"api", "plan"})

        created = client.post("/api/proxy/accounts", json={
            "name": "metered-main",
            "account_type": "api",
            "base_url": "http://upstream.test/v1",
            "api_format": "openai",
            "upstream_keys": ["sk-app-contract-secret"],
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        account_id = created.get_json()["id"]

        accounts = client.get("/api/proxy/accounts").get_json()
        account = next(item for item in accounts if item["id"] == account_id)
        self.assertEqual(account["account_type"], "api")
        self.assertEqual(account["is_aggregate"], 0)

        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT major,minor FROM schema_version WHERE id=1"
            ).fetchone(), (1, 18))
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM accounts WHERE id=?", (account_id,)
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_rules WHERE route_set_id=?", (account_id,)
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_secrets s JOIN upstream_credentials c "
                "ON c.uuid=s.credential_uuid JOIN upstreams u ON u.id=c.upstream_id "
                "WHERE u.account_id=?", (account_id,)
            ).fetchone()[0], 1)

    def test_timeout_config_can_be_saved_through_http(self) -> None:
        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        client = app.test_client()
        payload = {
            "anthropic": {
                "streaming_first_byte_timeout": 91,
                "streaming_idle_timeout": 181,
                "non_streaming_timeout": 601,
            },
            "openai_responses": {
                "streaming_first_byte_timeout": 61,
                "streaming_idle_timeout": 121,
                "non_streaming_timeout": 601,
            },
            "openai": {
                "streaming_first_byte_timeout": 62,
                "streaming_idle_timeout": 122,
                "non_streaming_timeout": 602,
            },
        }

        saved = client.put("/api/proxy/timeout-config", json=payload)
        self.assertEqual(saved.status_code, 200,
                         saved.get_data(as_text=True))
        self.assertEqual(saved.get_json(), {"status": "ok"})

        config = client.get("/api/proxy/timeout-config")
        self.assertEqual(config.status_code, 200)
        expected = {
            group: {"app_type": group, **values}
            for group, values in payload.items()
        }
        self.assertEqual(config.get_json(), expected)

    def test_pricing_reorder_endpoint_accepts_complete_order(self) -> None:
        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        client = app.test_client()
        first = client.post("/api/proxy/pricing", json={
            "model_pattern": "first-*", "input_price": 1,
            "output_price": 2,
        }).get_json()["id"]
        second = client.post("/api/proxy/pricing", json={
            "model_pattern": "second-*", "input_price": 3,
            "output_price": 4,
        }).get_json()["id"]

        response = client.post("/api/proxy/pricing/reorder",
                               json={"ids": [second, first]})
        self.assertEqual(response.status_code, 200,
                         response.get_data(as_text=True))
        self.assertEqual(
            [row["id"] for row in client.get("/api/proxy/pricing").get_json()],
            [second, first],
        )
        stale = client.post("/api/proxy/pricing/reorder", json={"ids": [first]})
        self.assertEqual(stale.status_code, 400)

    def test_dashboard_facade_uses_v1_grain(self) -> None:
        dashboard = DashboardDatabase(
            str(self.dashboard_path), str(self.root / "schema"))
        with sqlite3.connect(self.dashboard_path) as conn:
            conn.execute(
                "INSERT INTO accounts(account_id,name) VALUES(7,'archive-account')")
        dashboard.upsert_proxy_batch([{
            "date": "2026-08-09", "account_id": 7, "model": "model-a",
            "prompt_tokens": 100, "cache_read_tokens": 20,
            "completion_tokens": 30, "request_count": 2,
            "cost": 1.25, "billed_usage_cost": 1.0,
        }])
        rows = dashboard.load_rows()
        self.assertEqual(rows[1][0]["count"], 2)
        self.assertAlmostEqual(rows[2][0]["cost"], 1.25)
        self.assertAlmostEqual(rows[2][0]["actual_cost"], 1.0)
        self.assertAlmostEqual(rows[2][0]["theoretical_cost"], 1.25)

    def test_account_templates_aggregate_and_credential_identity(self) -> None:
        database = self.proxy_database()
        dated = database.create_account({
            "name": "dated", "account_type": "api",
            "base_url": "http://dated.test", "valid_from": "2025-01-15",
        })
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM billing_contracts WHERE account_id=?",
                (dated,)).fetchone()[0], "2025-01-15T00:00:00Z")
        self.assertTrue(database.update_account(dated, {"valid_from": "2025-02-01"}))
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT valid_from FROM billing_contracts WHERE account_id=?",
                (dated,)).fetchone()[0], "2025-02-01T00:00:00Z")
        metered = database.create_account({
            "name": "metered", "account_type": "api",
            "base_url": "http://metered.test",
            "upstream_keys": ["sk-same-prefix-111111-tail"],
        })
        recurring = database.create_account({
            "name": "recurring", "account_type": "plan",
            "base_url": "http://recurring.test", "monthly_price": 20,
            "upstream_keys": ["sk-same-prefix-222222-tail"],
        })
        software_id = database.create_agent_software({
            "name": "imported", "agent_kind": "codex",
        })
        subscription_id = database.create_agent_subscription({
            "name": "codex-subscription", "valid_from": "2026-08-01",
            "monthly_price": 10, "currency": "CNY",
        })
        aggregate = database.create_aggregate({
            "name": "combined", "entries": [{
                "pattern": "model-*", "account_id": metered,
                "upstream_model": "model-target",
            }],
        })
        database.create_key({
            "key_value": "tb-combined", "label": "combined",
            "account_id": aggregate,
        })
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT charge_type FROM billing_contracts WHERE account_id=?",
                (metered,)).fetchone()[0], "metered")
            self.assertEqual(conn.execute(
                "SELECT charge_type FROM billing_contracts WHERE account_id=?",
                (recurring,)).fetchone()[0], "recurring")
            self.assertEqual(conn.execute(
                "SELECT agent_kind FROM agent_software WHERE id=?",
                (software_id,)).fetchone()[0], "codex")
            self.assertEqual(conn.execute(
                "SELECT name FROM agent_subscriptions WHERE id=?",
                (subscription_id,)).fetchone()[0], "codex-subscription")
            self.assertEqual(conn.execute(
                "SELECT account_id FROM route_sets WHERE id=?",
                (aggregate,)).fetchone()[0], None)
            credentials = conn.execute(
                "SELECT uuid,key_masked FROM upstream_credentials ORDER BY uuid"
            ).fetchall()
            self.assertEqual(len(credentials), 2)
            self.assertEqual(len({row[0] for row in credentials}), 2)

    def test_pending_account_deletion_can_be_cancelled_over_http(self) -> None:
        database = self.proxy_database()
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", "end_of_period"),
            )
            conn.commit()
        account_id = database.create_account({
            "name": "http-restorable-plan", "account_type": "plan",
            "valid_from": "2026-08-01", "monthly_price": 20,
            "base_url": "http://example.test", "upstream_keys": ["sk-http-restore"],
            "new_valid_froms": ["2026-08-01"],
        })
        fixed_now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        from app.db.proxy.billing import materialize_period_charges
        materialize_period_charges(str(self.proxy_path), fixed_now)
        with patch("app.db.proxy.lifecycle.utc_now", return_value=fixed_now):
            self.assertTrue(database.delete_account(account_id)["deferred"])
            app = create_app(str(self.proxy_path), testing=True,
                             start_background_tasks=False)
            response = app.test_client().post(
                f"/api/proxy/accounts/{account_id}/cancel-deletion")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["status"], "ok")
        self.assertEqual(response.get_json()["restored_credentials"], 1)
        restored = next(row for row in database.get_accounts()
                        if row["id"] == account_id)
        self.assertIsNone(restored.get("deleted_at"))

    def test_agent_subscription_has_no_public_lifecycle_status(self) -> None:
        database = self.proxy_database()
        subscription_id = database.create_agent_subscription({
            "name": "status-free-subscription", "valid_from": "2026-08-01",
            "monthly_price": 20, "currency": "USD",
        })
        rows = database.get_agent_subscriptions()
        row = next(item for item in rows if item["id"] == subscription_id)
        self.assertNotIn("lifecycle_state", row)
        self.assertNotIn("valid_until", row)
        self.assertTrue(database.update_agent_subscription(subscription_id, {
            "name": "renamed-subscription", "monthly_price": 21,
        }))

    def test_account_key_count_deduplicates_cloud_metadata(self) -> None:
        database = self.proxy_database()
        account_id = database.create_account({
            "name": "deduplicated-plan", "account_type": "plan",
            "base_url": "http://dedup.test", "monthly_price": 20,
            "upstream_keys": ["sk-dedup-local"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            upstream_id = conn.execute(
                "SELECT id FROM upstreams WHERE account_id=?", (account_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO upstream_credentials"
                "(uuid,runtime_id,upstream_id,position,key_masked) "
                "VALUES('cloud-duplicate',999,?,?,?)",
                (upstream_id, 1, "sk-ded…ocal"),
            )
        account = next(item for item in database.get_accounts()
                       if item["id"] == account_id)
        self.assertEqual(account["key_count"], 1)

    def test_actual_and_theoretical_cost_are_not_added_together(self) -> None:
        database = self.proxy_database()
        account_id = database.create_account({
            "name": "cost-account", "account_type": "api",
            "base_url": "http://cost.test", "upstream_keys": ["sk-cost"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "equivalent_cost,billed_usage_cost,status_code,requested_at,"
                "pricing_status) VALUES('cost-fixture','import',?,'model',7,3,"
                "200,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'frozen')",
                (account_id,))
            contract_id = conn.execute(
                "SELECT id FROM billing_contracts WHERE account_id=?",
                (account_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO billing_period_charges(contract_id,period_start,"
                "period_end,recurring_charge,currency,normalized_recurring_cost) "
                "VALUES(?,strftime('%Y-%m-%dT00:00:00Z','now','-1 day'),"
                "strftime('%Y-%m-%dT00:00:00Z','now','+1 day'),2,'CNY',2)",
                (contract_id,))
        stats = database.get_stats()
        self.assertEqual(stats["total_cost"], 5.0)
        self.assertEqual(stats["today_cost"], 7.0)

    def test_routing_mutations_advance_snapshot_generation(self) -> None:
        database = self.proxy_database()
        account_id = database.create_account({
            "name": "generation-account", "account_type": "api",
            "base_url": "http://generation.test", "upstream_keys": ["sk-generation"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            before = conn.execute(
                "SELECT generation FROM config_state WHERE id=1").fetchone()[0]
            conn.execute(
                "UPDATE upstreams SET endpoint_path='/v1/changed' WHERE account_id=?",
                (account_id,))
            conn.commit()
            after = conn.execute(
                "SELECT generation FROM config_state WHERE id=1").fetchone()[0]
        self.assertGreater(after, before)

    def test_perf_health_and_background_shutdown_contract(self) -> None:
        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        response = app.test_client().get("/api/proxy/perf/realtime")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["background_health"], "ok")
        self.assertEqual(payload["background_tasks"], {})
        self.assertIn("accounting", payload)
        self.assertIn("transport", payload)
        self.assertEqual(payload["sync_health"], "unconfigured")

        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_state(key,value) VALUES('sync_health',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("sync upload degraded: conflict",),
            )
            conn.commit()
        degraded = app.test_client().get("/api/proxy/perf/realtime").get_json()
        self.assertEqual(degraded["sync_health"],
                         "sync upload degraded: conflict")
        self.assertEqual(degraded["background_health"], "degraded")

        from app.services.runtime_tasks import stop_runtime_tasks
        stop = threading.Event()
        finished = threading.Event()

        def worker() -> None:
            stop.wait()
            finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        app.config["TEST_STOP"] = stop
        app.config["BACKGROUND_TASK_THREADS"] = [thread]
        stop_runtime_tasks(app, join_timeout=1.0)
        self.assertTrue(finished.wait(0.1))
        self.assertFalse(thread.is_alive())

    def test_time_conventions_are_iso_only(self) -> None:
        parsed = parse_runtime_timestamp("2026-08-04T16:37:44Z")
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.hour, 16)
        # The legacy SQLite space format is no longer accepted at runtime.
        with self.assertRaises(ValueError):
            parse_runtime_timestamp("2026-08-04 16:37:44")

    def test_request_logs_use_simple_pagination(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "iso-filter", "account_type": "api",
            "base_url": "http://iso.test", "upstream_keys": ["sk-iso"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            for ts in ("2026-08-04T01:00:00Z", "2026-08-04T12:00:00Z",
                       "2026-08-05T01:00:00Z"):
                conn.execute(
                    "INSERT INTO request_log(event_id,source_kind,account_id,"
                    "model,status_code,requested_at,pricing_status) "
                    "VALUES(?,?,?,'model',200,?,'frozen')",
                    ("iso-filter:" + ts, "proxy", account_id, ts))
            conn.commit()
        result = db.get_request_logs(page=2, per_page=2)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["page"], 2)
        self.assertEqual([item["requested_at"] for item in result["items"]],
                         ["2026-08-04T01:00:00Z"])
        self.assertNotIn("project", result["items"][0])
        self.assertNotIn("session_id", result["items"][0])

    def test_perf_bucket_is_iso(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "iso-perf", "account_type": "api",
            "base_url": "http://perf.test", "upstream_keys": ["sk-perf"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "model,status_code,requested_at,pricing_status) "
                "VALUES('iso-perf','proxy',?,'model',200,"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'frozen')",
                (account_id,))
            conn.commit()
        rows = db.get_perf_throughput(60)
        self.assertTrue(rows)
        import re
        self.assertRegex(rows[0]["bucket"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z$")

    def test_account_key_and_credential_crud(self) -> None:
        database = self.proxy_database()
        account_id = database.create_account({
            "name": "crud", "account_type": "api",
            "base_url": "http://crud.test", "upstream_keys": ["sk-crud-1"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_credentials c JOIN upstreams u "
                "ON u.id=c.upstream_id WHERE u.account_id=?", (account_id,)
            ).fetchone()[0], 1)
            contract_id = conn.execute(
                "SELECT id FROM billing_contracts WHERE account_id=?",
                (account_id,)).fetchone()[0]

        # Account metadata update keeps the contract and credentials.
        self.assertTrue(database.update_account(account_id, {
            "name": "crud-renamed", "base_url": "http://crud2.test"}))
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT name FROM accounts WHERE id=?",
                (account_id,)).fetchone()[0], "crud-renamed")
            self.assertEqual(conn.execute(
                "SELECT id FROM billing_contracts WHERE id=?",
                (contract_id,)).fetchone()[0], contract_id)

        # Local client-key CRUD.
        key_value = database.create_key({
            "account_id": account_id, "label": "primary"})
        self.assertTrue(key_value)
        keys = database.get_keys()
        key_row = next(k for k in keys if k["key_value"] == key_value)
        self.assertEqual(key_row["account_id"], account_id)
        self.assertTrue(database.update_key(key_row["id"], {"label": "renamed"}))
        self.assertTrue(database.delete_key(key_row["id"]))
        self.assertNotIn(key_value, [k["key_value"] for k in database.get_keys()])

        # Immediate deletion removes the live row but retains the identity.
        result = database.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM account_identities WHERE id=?", (account_id,)
            ).fetchone())
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_credentials c JOIN upstreams u "
                "ON u.id=c.upstream_id WHERE u.account_id=? "
                "AND c.deleted_at IS NULL", (account_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_sets WHERE id=? AND enabled=1",
                (account_id,)).fetchone()[0], 0)
        self.assertNotIn(account_id, [a["id"] for a in database.get_accounts()])

    def test_agent_software_has_no_upstream_credential(self) -> None:
        database = self.proxy_database()
        software_id = database.create_agent_software({
            "name": "codex-agent", "agent_kind": "codex",
        })
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstreams WHERE account_id=?",
                (software_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT agent_kind FROM agent_software WHERE id=?",
                (software_id,)).fetchone()[0], "codex")
        self.assertEqual(database.get_agent_software()[0]["id"], software_id)
        self.assertNotIn(software_id, [account["id"] for account in database.get_accounts()])

    def test_agent_software_delete_is_soft(self) -> None:
        database = self.proxy_database()
        software_id = database.create_agent_software({
            "name": "soft-deleted-agent", "agent_kind": "codex",
        })
        subscription_id = database.create_agent_subscription({
            "name": "soft-deleted-subscription", "valid_from": "2026-08-01",
            "monthly_price": 10, "currency": "CNY",
        })
        self.assertTrue(database.update_agent_software(
            software_id, {"subscription_ids": [subscription_id]}))

        self.assertTrue(database.delete_agent_software(software_id))
        with sqlite3.connect(self.proxy_path) as conn:
            account = conn.execute(
                "SELECT lifecycle_state,deleted_at FROM accounts WHERE id=?",
                (software_id,)).fetchone()
            self.assertEqual(account[0], "deleted")
            self.assertIsNotNone(account[1])
            self.assertEqual(conn.execute(
                "SELECT enabled FROM agent_software WHERE id=?",
                (software_id,)).fetchone()[0], 0)
            self.assertIsNotNone(conn.execute(
                "SELECT software_id FROM agent_software_runtime WHERE software_id=?",
                (software_id,)).fetchone())
            binding = conn.execute(
                "SELECT lifecycle_state,valid_until FROM agent_subscription_bindings "
                "WHERE software_id=? AND subscription_id=?",
                (software_id, subscription_id),
            ).fetchone()
            self.assertEqual(binding[0], "deleted")
            self.assertIsNotNone(binding[1])
        self.assertEqual(database.get_agent_software(), [])

    def test_aggregate_route_set_and_model_catalog(self) -> None:
        database = self.proxy_database()
        metered = database.create_account({
            "name": "member", "account_type": "api",
            "base_url": "http://member.test", "upstream_keys": ["sk-member"],
        })
        aggregate = database.create_aggregate({
            "name": "combined", "entries": [{
                "pattern": "model-*", "account_id": metered,
                "upstream_model": "model-target",
            }, {
                "pattern": "other-*", "account_id": metered,
            }],
        })
        aggregates = database.get_aggregates()
        agg = next(a for a in aggregates if a["id"] == aggregate)
        self.assertEqual(len(agg["entries"]), 2)
        self.assertEqual(agg["entries"][0]["upstream_model"], "model-target")
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT account_id FROM route_sets WHERE id=?",
                (aggregate,)).fetchone()[0], None)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_rules WHERE route_set_id=? AND enabled=1",
                (aggregate,)).fetchone()[0], 2)
        self.assertTrue(database.update_aggregate(
            aggregate, {"entries": [{
                "pattern": "only-*", "account_id": metered}]}))
        self.assertEqual(len(database.get_aggregates()[0]["entries"]), 1)
        self.assertTrue(database.delete_aggregate(aggregate))

    def test_background_task_worker_lifecycle(self) -> None:
        from app.services.runtime_tasks import _periodic
        import time

        def wait_until(predicate, timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return True
                time.sleep(0.01)
            return predicate()

        calls: list[str] = []

        # A worker runs immediately on start, then periodically; an error on
        # one iteration degrades health but a later success recovers it.
        first = threading.Event()
        stop = threading.Event()
        health: dict = {}
        lock = threading.Lock()

        def action() -> None:
            if not calls:
                first.set()
            calls.append("tick")
            if len(calls) == 1:
                raise RuntimeError("boom")

        worker = threading.Thread(
            target=_periodic,
            args=(stop, 1, "test-worker", action, health, lock))
        worker.start()
        self.assertTrue(first.wait(2), "task did not run immediately on start")
        time.sleep(1.4)  # let the second periodic iteration run and succeed
        self.assertTrue(wait_until(
            lambda: health.get("test-worker", {}).get("status") == "ok"))
        self.assertGreaterEqual(len(calls), 2)
        stop.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(health["test-worker"]["status"], "stopped")
        self.assertNotIn("last_error", health["test-worker"])

        # A worker that keeps failing stays degraded with the error visible.
        stop2 = threading.Event()
        health2: dict = {}
        seen = threading.Event()

        def failing() -> None:
            seen.set()
            raise RuntimeError("always-fails")

        worker2 = threading.Thread(
            target=_periodic,
            args=(stop2, 1, "broken", failing, health2, lock))
        worker2.start()
        self.assertTrue(seen.wait(2))
        self.assertTrue(wait_until(
            lambda: health2.get("broken", {}).get("status") == "degraded"))
        self.assertEqual(health2["broken"].get("last_error"), "action failed")
        stop2.set()
        worker2.join(timeout=3)
        self.assertFalse(worker2.is_alive())


if __name__ == "__main__":
    import unittest
    unittest.main()
