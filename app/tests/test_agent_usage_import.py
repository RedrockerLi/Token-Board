"""服务器内置 Agent 用量导入的端到端与调度测试。

覆盖:单次导入、幂等、服务器 worker 启动/定时/浏览器唤醒与停止。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tests.support import AppDatabaseTestCase
from app.services import codex_import


SESSION_ID = "11111111-2222-3333-4444-555555555555"

SESSION_LINES = [
    json.dumps({"type": "session_meta",
                "payload": {"session_id": SESSION_ID}}),
    json.dumps({"type": "turn_context",
                "payload": {"model": "gpt-5.1-codex"}}),
    json.dumps({"type": "event_msg",
                "timestamp": "2026-08-13T01:00:00.000Z",
                "payload": {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 10, "output_tokens": 20,
                    "cached_input_tokens": 5, "total_tokens": 35}}}}),
    json.dumps({"type": "event_msg",
                "timestamp": "2026-08-13T01:01:00.000Z",
                "payload": {"type": "token_count", "info": {"last_token_usage": {
                    "input_tokens": 1, "output_tokens": 2,
                    "cached_input_tokens": 0, "total_tokens": 3}}}}),
]


class AgentUsageImportTestCase(AppDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._sessions = tempfile.TemporaryDirectory()
        self.old_codx_dir = codex_import.CODEX_DIR
        codex_import.CODEX_DIR = Path(self._sessions.name)

    def tearDown(self) -> None:
        codex_import.CODEX_DIR = self.old_codx_dir
        self._sessions.cleanup()
        super().tearDown()

    def _write_session(self, name: str, lines) -> Path:
        day = Path(self._sessions.name) / "sessions" / "2026" / "08" / "13"
        day.mkdir(parents=True, exist_ok=True)
        path = day / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _create_software(self, database) -> int:
        return database.create_agent_software({
            "name": "codex-agent", "agent_kind": "codex",
            "config": {"data_root": self._sessions.name},
        })

    def _codex_rows(self) -> int:
        with sqlite3.connect(self.proxy_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM request_log WHERE event_id LIKE 'codex:%'"
            ).fetchone()[0]

    def _run_import(self) -> int:
        from app.db.proxy_db import ProxyDatabase
        return codex_import.import_once(ProxyDatabase(str(self.proxy_path)))

    def test_imports_rows_and_is_idempotent(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        db = ProxyDatabase(str(self.proxy_path))
        self._create_software(db)
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID),
                            SESSION_LINES)

        self.assertEqual(self._run_import(), 2)
        self.assertEqual(self._codex_rows(), 2)

        # 第二次运行幂等:不重复计数
        self.assertEqual(self._run_import(), 0)
        self.assertEqual(self._codex_rows(), 2)

        with sqlite3.connect(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT model,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,requested_at "
                "FROM request_log WHERE event_id LIKE 'codex:%' "
                "ORDER BY requested_at LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (
            "gpt-5.1-codex", 10, 20, 5, 35, "2026-08-13T01:00:00Z"))

    def test_fork_replay_history_is_not_counted_twice(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        def meta(timestamp, session_id, **extra):
            return json.dumps({
                "timestamp": timestamp,
                "type": "session_meta",
                "payload": {"id": session_id, "timestamp": timestamp, **extra},
            })

        def token(timestamp, input_tokens, output_tokens, total_tokens):
            usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                     "cached_input_tokens": 0,
                     "total_tokens": input_tokens + output_tokens}
            return json.dumps({
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "model": "gpt-5.2",
                    "last_token_usage": usage,
                    "total_token_usage": {"total_tokens": total_tokens},
                }},
            })

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        self._write_session("parent.jsonl", [
            meta("2026-08-13T01:00:00.000Z", "parent-session"),
            token("2026-08-13T01:01:00.000Z", 10, 1, 11),
            token("2026-08-13T01:02:00.000Z", 20, 2, 33),
        ])
        self._write_session("fork.jsonl", [
            meta("2026-08-13T01:30:00.000Z", "fork-session",
                 forked_from_id="parent-session"),
            token("2026-08-13T01:30:00.000Z", 20, 2, 33),
            token("2026-08-13T01:31:00.000Z", 5, 3, 41),
        ])

        self.assertEqual(self._run_import(), 3)
        with sqlite3.connect(self.proxy_path) as conn:
            prompt, completion = conn.execute(
                "SELECT SUM(prompt_tokens),SUM(completion_tokens) "
                "FROM request_log WHERE event_id LIKE 'codex:%'"
            ).fetchone()
        self.assertEqual((prompt, completion), (35, 6))

    def test_no_agent_account_returns_zero(self) -> None:
        # 无 codex agent 账户:干净返回 0,不报错
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID),
                            SESSION_LINES)
        self.assertEqual(self._run_import(), 0)
        self.assertEqual(self._codex_rows(), 0)

    def test_no_session_files_returns_zero(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        self._create_software(ProxyDatabase(str(self.proxy_path)))
        self.assertEqual(self._run_import(), 0)
        self.assertEqual(self._codex_rows(), 0)

    def test_parallel_passes_merge_cursor_paths(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID),
                            SESSION_LINES)
        second_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        second_lines = list(SESSION_LINES)
        second_lines[0] = json.dumps({
            "type": "session_meta", "payload": {"session_id": second_id}})
        self._write_session("rollout-20260813020000-{}.jsonl".format(second_id),
                            second_lines)

        barrier = threading.Barrier(2)
        original_parse = codex_import._parse_session

        def parse_in_lockstep(path, stop_event=None):
            result = original_parse(path, stop_event)
            barrier.wait(timeout=5)
            return result

        with patch.object(codex_import, "_parse_session", parse_in_lockstep):
            threads = [threading.Thread(target=lambda: self._run_import())
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        with sqlite3.connect(self.proxy_path) as conn:
            cursor = conn.execute(
                "SELECT cursor_json FROM agent_software_runtime "
                "LIMIT 1").fetchone()[0]
        states = json.loads(cursor)
        self.assertEqual(len(states), 2)
        self.assertEqual(self._codex_rows(), 4)

    def test_browser_endpoint_wakes_server_owned_worker(self) -> None:
        from app import create_app

        class FakeWorker:
            def __init__(self):
                self.calls = 0

            def trigger(self):
                self.calls += 1
                return True

        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        worker = FakeWorker()
        app.config["AGENT_USAGE_IMPORT_WORKER"] = worker

        response = app.test_client().post("/api/proxy/agent-usage/import")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"status": "scheduled"})
        self.assertEqual(worker.calls, 1)
        self.assertEqual(
            app.test_client().get("/api/proxy/agent-usage/import").status_code,
            405,
        )

    def test_explicit_schema_root_is_carried_into_sync_and_datastore(self) -> None:
        from app import create_app

        schema_root = self.root / "schema"
        app = create_app(
            str(self.proxy_path),
            schema_dir=str(schema_root),
            testing=True,
            start_background_tasks=False,
        )
        self.assertEqual(app.config["SCHEMA_DIR"], str(schema_root.resolve()))
        self.assertEqual(app.config["PROXY_DB"].schema_dir,
                         str(schema_root.resolve()))
        self.assertEqual(app.config["DATA_STORE"].schema_dir,
                         str(schema_root.resolve()))

    def test_server_lifecycle_imports_on_start_and_browser_open(self) -> None:
        from app import create_app
        from app.db.proxy_db import ProxyDatabase
        from app.services.runtime_tasks import stop_runtime_tasks

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        session_path = self._write_session(
            "rollout-20260813010000-{}.jsonl".format(SESSION_ID),
            SESSION_LINES,
        )

        def wait_for_rows(expected: int, timeout: float = 2.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._codex_rows() == expected:
                    return True
                time.sleep(0.01)
            return self._codex_rows() == expected

        with patch("app.services.fx.ensure_rate"), patch(
                "app.services.runtime_tasks.materialize_period_charges"):
            app = create_app(str(self.proxy_path), testing=True,
                             start_background_tasks=True)
            try:
                self.assertTrue(wait_for_rows(2),
                                "server startup did not import usage")
                extra = json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-08-13T01:02:00.000Z",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": 3, "output_tokens": 4,
                            "cached_input_tokens": 1, "total_tokens": 8,
                        },
                    }},
                })
                with session_path.open("a", encoding="utf-8") as handle:
                    handle.write(extra + "\n")

                response = app.test_client().post(
                    "/api/proxy/agent-usage/import")
                self.assertEqual(response.status_code, 202)
                self.assertTrue(wait_for_rows(3),
                                "browser open did not trigger another import")
            finally:
                stop_runtime_tasks(app)


class AgentUsageWorkerTest(unittest.TestCase):
    def test_pre_stopped_worker_does_not_start_an_import(self) -> None:
        from app.services.runtime_tasks import AgentUsageImportWorker

        calls = []
        worker = AgentUsageImportWorker(
            lambda: calls.append(True) or 0,
            interval=60,
            health={},
            health_lock=threading.Lock(),
        )
        worker.stop()
        worker.start()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(calls, [])

    def test_interval_is_exactly_thirty_minutes(self) -> None:
        from app.services.runtime_tasks import AGENT_USAGE_IMPORT_INTERVAL_SECONDS
        self.assertEqual(AGENT_USAGE_IMPORT_INTERVAL_SECONDS, 30 * 60)

    def test_startup_and_browser_triggers_are_serialized_and_coalesced(self) -> None:
        from app.services.runtime_tasks import AgentUsageImportWorker

        entered = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        state = {"calls": 0, "active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def action() -> int:
            with state_lock:
                state["calls"] += 1
                call_number = state["calls"]
                state["active"] += 1
                state["max_active"] = max(
                    state["max_active"], state["active"])
            try:
                if call_number == 1:
                    entered.set()
                    self.assertTrue(release.wait(2))
                elif call_number == 2:
                    second_done.set()
                return call_number
            finally:
                with state_lock:
                    state["active"] -= 1

        health = {}
        worker = AgentUsageImportWorker(
            action, interval=60, health=health,
            health_lock=threading.Lock())
        worker.start()
        self.assertTrue(entered.wait(2), "startup import did not run")
        for _ in range(10):
            self.assertTrue(worker.trigger())
        release.set()
        self.assertTrue(second_done.wait(2), "browser-triggered import did not run")
        time.sleep(0.05)
        worker.stop()
        worker.thread.join(timeout=2)

        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(state["calls"], 2)
        self.assertEqual(state["max_active"], 1)
        self.assertEqual(health["agent-usage-importer"]["status"], "stopped")

    def test_periodic_deadline_runs_another_pass(self) -> None:
        from app.services.runtime_tasks import AgentUsageImportWorker

        second_done = threading.Event()
        calls = []

        def action() -> int:
            calls.append(len(calls) + 1)
            if len(calls) >= 2:
                second_done.set()
            return 0

        worker = AgentUsageImportWorker(
            action, interval=0.05, health={},
            health_lock=threading.Lock())
        worker.start()
        self.assertTrue(second_done.wait(2), "periodic import did not run")
        worker.stop()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertGreaterEqual(len(calls), 2)

    def test_browser_trigger_does_not_postpone_periodic_deadline(self) -> None:
        from app.services.runtime_tasks import AgentUsageImportWorker

        calls = []
        third_done = threading.Event()

        def action() -> int:
            calls.append(time.monotonic())
            if len(calls) >= 3:
                third_done.set()
            return 0

        interval = 0.4
        worker = AgentUsageImportWorker(
            action, interval=interval, health={},
            health_lock=threading.Lock())
        worker.start()
        deadline = time.monotonic() + 2
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(calls), 1)
        time.sleep(interval / 2)
        self.assertTrue(worker.trigger())
        self.assertTrue(third_done.wait(2), "scheduled pass was postponed")
        worker.stop()
        worker.thread.join(timeout=2)

        # Startup at t0, browser pass near t0+0.2, scheduled pass near t0+0.4.
        # A fixed-delay implementation would not run the third pass until 0.6.
        self.assertLess(calls[2] - calls[0], interval * 1.35)

    def test_failure_health_recovers_and_dead_worker_rejects_triggers(self) -> None:
        from app.services.runtime_tasks import AgentUsageImportWorker

        attempted = threading.Event()
        calls = 0
        health = {}

        def action() -> int:
            nonlocal calls
            calls += 1
            attempted.set()
            if calls == 1:
                raise RuntimeError("broken import")
            return 7

        worker = AgentUsageImportWorker(
            action, interval=60, health=health,
            health_lock=threading.Lock())
        worker.start()
        self.assertTrue(attempted.wait(2))
        deadline = time.monotonic() + 2
        while (health.get("agent-usage-importer", {}).get("status") != "degraded"
               and time.monotonic() < deadline):
            time.sleep(0.005)
        self.assertEqual(health["agent-usage-importer"]["status"], "degraded")

        attempted.clear()
        self.assertTrue(worker.trigger())
        self.assertTrue(attempted.wait(2))
        deadline = time.monotonic() + 2
        while (health["agent-usage-importer"].get("status") != "ok"
               and time.monotonic() < deadline):
            time.sleep(0.005)
        self.assertEqual(health["agent-usage-importer"]["status"], "ok")
        self.assertEqual(health["agent-usage-importer"]["last_inserted"], 7)
        worker.stop()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.trigger())


if __name__ == "__main__":
    unittest.main()
