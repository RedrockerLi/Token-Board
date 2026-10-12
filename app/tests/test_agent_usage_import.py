"""token-maintenance Agent 用量导入的端到端与调度测试。

覆盖:单次导入、幂等、服务器 worker 启动/定时/浏览器唤醒与停止。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.tests.support import AppDatabaseTestCase, sqlite_connection
from app.services.agent_usage.adapters import codex as codex_adapter
from app.services.agent_usage.importer import import_once
from app.services.agent_usage.ir import ParseBatch, UsageEvent


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

    def tearDown(self) -> None:
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
        with sqlite_connection(self.proxy_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM request_log WHERE event_id LIKE 'codex:%'"
            ).fetchone()[0]

    def _run_import(self) -> int:
        from app.db.proxy_db import ProxyDatabase
        return import_once(ProxyDatabase(str(self.proxy_path)))

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

        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT model,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,requested_at "
                "FROM request_log WHERE event_id LIKE 'codex:%' "
                "ORDER BY requested_at LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (
            "gpt-5.1-codex", 10, 20, 5, 35, "2026-08-13T01:00:00Z"))

    def test_claude_import_preserves_cache_read_as_a_hit(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        root = Path(self._sessions.name) / "claude"
        session = root / "projects" / "-work-project" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "type": "assistant",
            "uuid": "claude-call-1",
            "timestamp": "2026-08-13T01:00:00Z",
            "cwd": "/work/project",
            "message": {
                "id": "claude-message-1",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 11,
                    "cache_read_input_tokens": 13,
                    "cache_creation_input_tokens": 17,
                    "output_tokens": 7,
                },
            },
        }) + "\n", encoding="utf-8")
        software_id = database.create_agent_software({
            "name": "claude-agent", "agent_kind": "claude-code",
            "config": {"data_root": str(root)},
        })

        self.assertEqual(import_once(database), 1)
        with sqlite_connection(self.proxy_path) as connection:
            row = connection.execute(
                "SELECT model,prompt_tokens,cache_read_tokens,"
                "completion_tokens,total_tokens,agent_software_id "
                "FROM request_log WHERE agent_software_id=?",
                (software_id,),
            ).fetchone()
        self.assertEqual(row, (
            "claude-opus-4-8", 41, 13, 7, 48, software_id))

    def _claude_session(self, prefix: str, lines) -> tuple[int, Path]:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        root = Path(self._sessions.name) / prefix
        session = root / "projects" / "-work-project" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n",
            encoding="utf-8")
        software_id = database.create_agent_software({
            "name": f"{prefix}-agent", "agent_kind": "claude-code",
            "config": {"data_root": str(root)},
        })
        return software_id, session

    def test_claude_import_estimates_duration_and_output_tps(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        software_id, _ = self._claude_session("claude-tps", [
            {"type": "user", "uuid": "tps-1", "parentUuid": None,
             "timestamp": "2026-08-13T01:00:00.000Z"},
            {"type": "assistant", "uuid": "tps-2", "parentUuid": "tps-1",
             "apiBlockIndex": 0, "timestamp": "2026-08-13T01:00:02.500Z",
             "cwd": "/work/project",
             "message": {"id": "tps-message-1", "model": "claude-opus-4-8",
                         "usage": {"input_tokens": 10, "output_tokens": 300},
                         "stop_reason": "end_turn"}},
        ])

        self.assertEqual(import_once(ProxyDatabase(str(self.proxy_path))), 1)
        with sqlite_connection(self.proxy_path) as connection:
            row = connection.execute(
                "SELECT duration_ms,output_tps,completion_tokens "
                "FROM request_log WHERE agent_software_id=?",
                (software_id,),
            ).fetchone()
        self.assertEqual(row[0], 2500)
        self.assertEqual(row[2], 300)
        self.assertAlmostEqual(row[1], 300 / (2500 / 1000))

        # 再导入一次幂等:不重复插行,已有耗时不变
        self.assertEqual(import_once(ProxyDatabase(str(self.proxy_path))), 0)
        with sqlite_connection(self.proxy_path) as connection:
            again = connection.execute(
                "SELECT count(*),max(duration_ms),max(output_tps) "
                "FROM request_log WHERE agent_software_id=?",
                (software_id,),
            ).fetchone()
        self.assertEqual(again, (1, 2500, 120.0))

    def test_claude_import_keeps_duration_without_tps_below_threshold(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        software_id, _ = self._claude_session("claude-short", [
            {"type": "user", "uuid": "short-1", "parentUuid": None,
             "timestamp": "2026-08-13T02:00:00.000Z"},
            {"type": "assistant", "uuid": "short-2", "parentUuid": "short-1",
             "timestamp": "2026-08-13T02:00:03.000Z",
             "message": {"id": "short-message-1", "model": "claude-opus-4-8",
                         "usage": {"input_tokens": 10, "output_tokens": 150},
                         "stop_reason": "end_turn"}},
        ])

        self.assertEqual(import_once(ProxyDatabase(str(self.proxy_path))), 1)
        with sqlite_connection(self.proxy_path) as connection:
            row = connection.execute(
                "SELECT duration_ms,output_tps FROM request_log "
                "WHERE agent_software_id=?",
                (software_id,),
            ).fetchone()
        # 输出不到 200 token 不估速度,原生的估算耗时仍然入库
        self.assertEqual(row, (3000, 0.0))

    def test_claude_import_backfills_duration_when_the_reply_finishes_later(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        software_id, session = self._claude_session("claude-backfill", [
            {"type": "user", "uuid": "bf-1", "parentUuid": None,
             "timestamp": "2026-08-13T03:00:00.000Z"},
            # 扫描瞬间回复尚未写完:没有 stop_reason,先不估耗时
            {"type": "assistant", "uuid": "bf-2", "parentUuid": "bf-1",
             "apiBlockIndex": 0, "timestamp": "2026-08-13T03:00:02.000Z",
             "message": {"id": "bf-message-1", "model": "claude-opus-4-8",
                         "usage": {"input_tokens": 10, "output_tokens": 300},
                         "stop_reason": None}},
        ])

        self.assertEqual(import_once(ProxyDatabase(str(self.proxy_path))), 1)
        with sqlite_connection(self.proxy_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT duration_ms,output_tps FROM request_log "
                "WHERE agent_software_id=?",
                (software_id,),
            ).fetchone(), (0, 0.0))

        # 最终块落盘后重扫:同一 message.id 不重复插行,只把耗时补上
        with session.open("a", encoding="utf-8") as target:
            target.write(json.dumps({
                "type": "assistant", "uuid": "bf-3", "parentUuid": "bf-2",
                "apiBlockIndex": 1, "timestamp": "2026-08-13T03:00:02.500Z",
                "message": {"id": "bf-message-1", "model": "claude-opus-4-8",
                            "usage": {"input_tokens": 10, "output_tokens": 300},
                            "stop_reason": "end_turn"},
            }) + "\n")
        self.assertEqual(import_once(ProxyDatabase(str(self.proxy_path))), 0)
        with sqlite_connection(self.proxy_path) as connection:
            rows = connection.execute(
                "SELECT duration_ms,output_tps FROM request_log "
                "WHERE agent_software_id=?",
                (software_id,),
            ).fetchall()
        self.assertEqual(rows, [(2500, 120.0)])

    def test_hermes_history_stays_deduplicated_after_log_retention(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        hermes_root = Path(self._sessions.name) / "hermes"
        hermes_root.mkdir()
        hermes_db = hermes_root / "state.db"
        with sqlite_connection(hermes_db) as connection:
            connection.execute("""CREATE TABLE sessions (
                id TEXT, model TEXT, started_at TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER,
                reasoning_tokens INTEGER, cache_write_tokens INTEGER
            )""")
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                ("old-session", "hermes-model", "2020-01-01T00:00:00Z",
                 10, 5, 0, 0, 0),
            )
        software_id = database.create_agent_software({
            "name": "hermes-agent", "agent_kind": "hermes",
            "config": {"data_root": str(hermes_root)},
        })

        self.assertEqual(import_once(database), 1)
        with sqlite_connection(self.proxy_path) as connection:
            max_id = connection.execute(
                "SELECT max(id) FROM request_log"
            ).fetchone()[0]
        self.assertGreater(max_id, 0)
        self.assertEqual(database.cleanup_exported_logs(max_id), 1)
        self.assertEqual(import_once(database), 0)
        with sqlite_connection(self.proxy_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM request_log WHERE agent_software_id=?",
                (software_id,),
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM agent_usage_receipts"
            ).fetchone()[0], 1)
        self.assertTrue(database.delete_agent_software(software_id))
        with sqlite_connection(self.proxy_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM agent_usage_receipts"
            ).fetchone()[0], 1)

    def test_agent_context_is_not_persisted(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        software_id = self._create_software(database)
        path = self._write_session("context.jsonl", ["placeholder"])
        event = UsageEvent.from_buckets(
            model="context-model", input_tokens=10, output_tokens=2,
            requested_at="2026-08-13T01:00:00Z", event_id="codex:context",
            project="private-project", session_id="private-session",
        )

        def parser(source_path, stop_event=None):
            self.assertEqual(source_path, path)
            return ParseBatch.from_events([event], 1)

        self.assertEqual(import_once(
            database, parser_overrides={"codex": parser}), 1)
        self.assertTrue(database.insert_agent_usage(
            software_id, "legacy-context", 1, 1, 0, 2,
            "2026-08-13T01:01:00Z", "codex:legacy-context"))
        with self.assertRaises(TypeError):
            database.insert_agent_usage(
                software_id, "rejected-context", 1, 1, 0, 2,
                "2026-08-13T01:02:00Z", "codex:rejected-context",
                project="private-project", session_id="private-session")
        with sqlite_connection(self.proxy_path) as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(request_log)")}
            row = conn.execute(
                "SELECT model,prompt_tokens,completion_tokens,total_tokens "
                "FROM request_log WHERE event_id='codex:context'"
            ).fetchone()
        self.assertNotIn("project", columns)
        self.assertNotIn("session_id", columns)
        self.assertEqual(row, ("context-model", 10, 2, 12))

    def test_skipped_batch_inserts_partial_events_but_preserves_cursor(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        path = self._write_session("rollout-skipped.jsonl", ["placeholder"])
        event = UsageEvent.from_buckets(
            model="gpt-partial", input_tokens=10, output_tokens=2,
            requested_at="2026-08-13T01:00:00Z", event_id="adapter:partial",
        )

        def parser(source_path, stop_event=None):
            self.assertEqual(source_path, path)
            return ParseBatch.from_events(
                [event], 1, skipped=True,
                warnings=("source is still being written",),
            )

        self.assertEqual(import_once(database, parser_overrides={"codex": parser}), 1)
        with sqlite_connection(self.proxy_path) as conn:
            cursor = conn.execute(
                "SELECT cursor_json,last_error FROM agent_software_runtime"
            ).fetchone()
        self.assertEqual(json.loads(cursor[0]), {})
        self.assertEqual(cursor[1], "source is still being written")

    def test_opencode_custom_sqlite_filename_uses_native_adapter(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode-data.sqlite"
            with sqlite_connection(path) as connection:
                connection.execute("CREATE TABLE message(data TEXT, session_id TEXT)")
                connection.execute(
                    "INSERT INTO message(data,session_id) VALUES(?,?)",
                    (json.dumps({
                        "role": "assistant",
                        "time": {"created": 1782720000000},
                        "modelID": "opencode-model",
                        "tokens": {"input": 10, "output": 2,
                                    "reasoning": 1, "cache": {"read": 5}},
                        "path": {"root": "/work/project"},
                    }), "session-1"),
                )
            database.create_agent_software({
                "name": "opencode-agent", "agent_kind": "opencode",
                "config": {"data_root": str(path)},
            })

            self.assertEqual(import_once(database), 1)
            with sqlite_connection(self.proxy_path) as connection:
                row = connection.execute(
                    "SELECT model,prompt_tokens,completion_tokens,"
                    "cache_read_tokens,total_tokens "
                    "FROM request_log WHERE event_id LIKE 'opencode:%'"
                ).fetchone()
            self.assertEqual(tuple(row),
                             ("opencode-model", 10, 3, 5, 13))

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
        with sqlite_connection(self.proxy_path) as conn:
            prompt, completion = conn.execute(
                "SELECT SUM(prompt_tokens),SUM(completion_tokens) "
                "FROM request_log WHERE event_id LIKE 'codex:%'"
            ).fetchone()
        self.assertEqual((prompt, completion), (35, 6))

    def test_estimated_duration_and_output_speed_are_imported_idempotently(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID), [
            json.dumps({"type": "session_meta",
                        "payload": {"session_id": SESSION_ID}}),
            json.dumps({"type": "turn_context",
                        "payload": {"model": "gpt-5.1-codex"}}),
            json.dumps({"timestamp": "2026-08-13T01:00:00.000Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"}}),
            json.dumps({"timestamp": "2026-08-13T01:00:05.000Z",
                        "type": "response_item",
                        "payload": {"type": "reasoning"}}),
            json.dumps({"timestamp": "2026-08-13T01:00:10.000Z",
                        "type": "event_msg",
                        "payload": {"type": "token_count", "info": {
                            "last_token_usage": {
                                "input_tokens": 100, "output_tokens": 500,
                                "cached_input_tokens": 0, "total_tokens": 600},
                            "total_token_usage": {"total_tokens": 600}}}}),
        ])

        self.assertEqual(self._run_import(), 1)
        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT duration_ms,output_tps,completion_tokens,total_tokens "
                "FROM request_log WHERE event_id LIKE 'codex:%'").fetchone()
        # 用户消息 01:00:00 → token_count 01:00:10:估算耗时 10 秒
        self.assertEqual((row[0], row[2], row[3]), (10_000, 500, 600))
        self.assertAlmostEqual(row[1], 500 * 1000.0 / 10_000, places=6)

        # 第二次导入幂等:不重复插入,也不改动已估算的耗时与速度
        self.assertEqual(self._run_import(), 0)
        with sqlite_connection(self.proxy_path) as conn:
            again = conn.execute(
                "SELECT duration_ms,output_tps FROM request_log "
                "WHERE event_id LIKE 'codex:%'").fetchone()
        self.assertEqual(tuple(again), (10_000, 50.0))

    def test_durable_usage_record_only_session_is_imported(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        self._create_software(database)
        usage = {"input_tokens": 100, "output_tokens": 600,
                 "cached_input_tokens": 0, "reasoning_output_tokens": 0,
                 "total_tokens": 700}
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID), [
            json.dumps({"type": "session_meta",
                        "payload": {"session_id": SESSION_ID}}),
            json.dumps({"type": "turn_context",
                        "payload": {"model": "gpt-5.1-codex"}}),
            json.dumps({"timestamp": "2026-08-13T01:00:01.000Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user"}}),
            json.dumps({"timestamp": "2026-08-13T01:00:05.000Z",
                        "type": "response_item",
                        "payload": {"type": "reasoning"}}),
            # 中断的调用只留下 durable 记录,没有 UI token_count
            json.dumps({"timestamp": "2026-08-13T01:00:09.500Z",
                        "type": "token_usage_record",
                        "payload": {"usage": usage,
                                    "thread_token_usage": dict(usage)}}),
        ])

        self.assertEqual(self._run_import(), 1)
        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT prompt_tokens,completion_tokens,total_tokens,"
                "duration_ms,output_tps FROM request_log "
                "WHERE event_id LIKE 'codex:%'").fetchone()
        self.assertEqual(tuple(row[:3]), (100, 600, 700))
        # 耗时按 token_usage_record 的时间戳结算(用户消息 01.000 → 记录 09.500)
        self.assertEqual(row[3], 8_500)
        self.assertAlmostEqual(row[4], 600 * 1000.0 / 8_500, places=6)

        self.assertEqual(self._run_import(), 0)
        with sqlite_connection(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM request_log "
                "WHERE event_id LIKE 'codex:%'").fetchone()[0], 1)

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
        original_parse = codex_adapter.parse

        def parse_in_lockstep(item, stop_event=None, *, skip_token_count=0,
                              **kwargs):
            result = original_parse(
                item, stop_event, skip_token_count=skip_token_count, **kwargs)
            barrier.wait(timeout=5)
            return result

        with patch.object(codex_adapter, "parse", parse_in_lockstep):
            threads = [threading.Thread(target=lambda: self._run_import())
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        with sqlite_connection(self.proxy_path) as conn:
            cursor = conn.execute(
                "SELECT cursor_json FROM agent_software_runtime "
                "LIMIT 1").fetchone()[0]
        states = json.loads(cursor)
        self.assertEqual(len(states), 2)
        self.assertEqual(self._codex_rows(), 4)

    def test_browser_endpoint_wakes_maintenance_owned_worker(self) -> None:
        import socket

        from app import create_app

        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=False)
        with patch("app.services.runtime_tasks.socket.socket") as factory:
            response = app.test_client().post("/api/proxy/agent-usage/import")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"status": "scheduled"})
        factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_DGRAM)
        client = factory.return_value.__enter__.return_value
        client.settimeout.assert_called_once_with(0.2)
        client.sendto.assert_called_once_with(
            b"IMPORT", app.config["MAINTENANCE_SOCKET"])
        self.assertNotIn("AGENT_USAGE_IMPORT_WORKER", app.config)
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
        self.assertEqual(app.config["TOKEN_BOARD_DB"].schema_dir,
                         str(schema_root.resolve()))
        self.assertEqual(app.config["DATA_STORE"].schema_dir,
                         str(schema_root.resolve()))

    def test_testing_app_does_not_start_maintenance_workers(self) -> None:
        from app import create_app

        app = create_app(str(self.proxy_path), testing=True,
                         start_background_tasks=True)
        self.assertNotIn("AGENT_USAGE_IMPORT_WORKER", app.config)
        self.assertNotIn("BACKGROUND_TASK_THREADS", app.config)

    def test_server_lifecycle_imports_on_start_and_browser_open(self) -> None:
        from app import create_app
        from app.db.proxy_db import ProxyDatabase
        from maintenance import MaintenanceService

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

        socket_path = self.root / "data" / "token-maintenance.sock"
        health_path = self.root / "data" / "token-maintenance-health.json"
        service = MaintenanceService(
            str(self.proxy_path), str(self.root / "schema"),
            str(socket_path), str(health_path))
        with patch("maintenance.fx.ensure_rate"), patch(
                "maintenance.materialize_all_period_charges"):
            service.start()
            app = create_app(str(self.proxy_path), testing=True,
                             start_background_tasks=False,
                             schema_dir=str(self.root / "schema"))
            try:
                self.assertTrue(wait_for_rows(2),
                                "maintenance startup did not import usage")
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
                                "maintenance socket did not trigger another import")
            finally:
                service.stop()


    # ── antigravity:合成临时 .db 数据根,走真实导入路径 ──

    @staticmethod
    def _pb_varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    @classmethod
    def _pb_varint_field(cls, number: int, value: int) -> bytes:
        return cls._pb_varint((number << 3) | 0) + cls._pb_varint(value)

    @classmethod
    def _pb_bytes_field(cls, number: int, payload: bytes) -> bytes:
        return (cls._pb_varint((number << 3) | 2)
                + cls._pb_varint(len(payload)) + payload)

    @classmethod
    def _pb_string_field(cls, number: int, text: str) -> bytes:
        return cls._pb_bytes_field(number, text.encode("utf-8"))

    def test_antigravity_import_counts_thinking_once_and_merges_both_tables(self) -> None:
        from app.db.proxy_db import ProxyDatabase

        database = ProxyDatabase(str(self.proxy_path))
        conversations = Path(self._sessions.name) / "antigravity" / "conversations"
        conversations.mkdir(parents=True)

        # 字段 3 是输出总量(可见 40 + thinking 80),字段 4 是未分类的
        # cache-creation;完成量必须仍是 120,thinking 不能计两次。
        gen_usage = b"".join([
            self._pb_varint_field(2, 900),
            self._pb_varint_field(3, 120),
            self._pb_varint_field(4, 700),
            self._pb_varint_field(5, 300),
            self._pb_varint_field(9, 80),
            self._pb_string_field(11, "RESP_E2E"),
        ])
        gen_blob = self._pb_bytes_field(1, b"".join([
            self._pb_bytes_field(4, gen_usage),
            self._pb_bytes_field(
                9, self._pb_bytes_field(4, self._pb_varint_field(1, 1783508703))),
            self._pb_string_field(21, "Gemini 3.8 Flash (High)"),
        ]))
        # 同一次调用(同一 response id)也被 steps 表看到:必须合并而不是第二行。
        step_usage = b"".join([
            self._pb_varint_field(2, 900),
            self._pb_varint_field(3, 120),
            self._pb_varint_field(5, 300),
            self._pb_varint_field(9, 80),
            self._pb_string_field(11, "RESP_E2E"),
        ])
        user_step = (self._pb_bytes_field(1, self._pb_varint_field(1, 1783508701))
                     + self._pb_varint_field(3, 4))
        assistant_step = (
            self._pb_bytes_field(1, self._pb_varint_field(1, 1783508703))
            + self._pb_varint_field(3, 2)
            + self._pb_bytes_field(9, step_usage))
        with sqlite_connection(conversations / "cascade-e2e.db") as connection:
            connection.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            connection.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
            connection.execute("INSERT INTO gen_metadata VALUES (?,?)", (1, gen_blob))
            connection.execute("INSERT INTO steps VALUES (?,?)", (1, user_step))
            connection.execute("INSERT INTO steps VALUES (?,?)", (2, assistant_step))

        software_id = database.create_agent_software({
            "name": "antigravity-agent", "agent_kind": "antigravity",
            "config": {"data_root": str(conversations)},
        })

        self.assertEqual(import_once(database), 1)
        with sqlite_connection(self.proxy_path) as connection:
            rows = connection.execute(
                "SELECT model,prompt_tokens,completion_tokens,cache_read_tokens,"
                "total_tokens,agent_software_id,requested_at FROM request_log "
                "WHERE agent_software_id=? ORDER BY id", (software_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), (
            "Gemini 3.8 Flash (High)", 1900, 120, 300, 2020, software_id,
            "2026-07-08T11:05:03Z"))

        # 第二次导入幂等:同一 response id 只有一行。
        self.assertEqual(import_once(database), 0)
        with sqlite_connection(self.proxy_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM request_log WHERE agent_software_id=?",
                (software_id,)).fetchone()[0], 1)


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
