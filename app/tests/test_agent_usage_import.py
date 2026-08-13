"""CLI 级测试:独立用量导入 (agent_usage_import.py) 端到端行为。

覆盖:one-shot 导入写入 request_log、重复运行幂等(0 新增)、
无 codex agent 账户/无会话文件时干净返回 0。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
        day = Path(self._sessions.name) / "2026" / "08" / "13"
        day.mkdir(parents=True, exist_ok=True)
        path = day / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _codex_rows(self) -> int:
        with sqlite3.connect(self.proxy_path) as conn:
            return conn.execute(
                "SELECT count(*) FROM request_log WHERE event_id LIKE 'codex:%'"
            ).fetchone()[0]

    def _run_main(self) -> int:
        from agent_usage_import import main
        return main([
            "--proxy-db", str(self.proxy_path),
            "--schema-dir", str(self.root / "schema"),
        ])

    def test_imports_rows_and_is_idempotent(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        db = ProxyDatabase(str(self.proxy_path))
        db.create_account({
            "name": "codex-agent", "account_type": "agent",
            "agent_kind": "codex", "monthly_price": 10,
        })
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID),
                            SESSION_LINES)

        self.assertEqual(self._run_main(), 0)
        self.assertEqual(self._codex_rows(), 2)

        # 第二次运行幂等:不重复计数
        self.assertEqual(self._run_main(), 0)
        self.assertEqual(self._codex_rows(), 2)

        with sqlite3.connect(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT model,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,requested_at "
                "FROM request_log WHERE event_id LIKE 'codex:%' "
                "ORDER BY requested_at LIMIT 1").fetchone()
        self.assertEqual(tuple(row), (
            "gpt-5.1-codex", 10, 20, 5, 35, "2026-08-13T01:00:00Z"))

    def test_no_agent_account_returns_zero(self) -> None:
        # 无 codex agent 账户:干净返回 0,不报错
        self._write_session("rollout-20260813010000-{}.jsonl".format(SESSION_ID),
                            SESSION_LINES)
        self.assertEqual(self._run_main(), 0)
        self.assertEqual(self._codex_rows(), 0)

    def test_no_session_files_returns_zero(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        ProxyDatabase(str(self.proxy_path)).create_account({
            "name": "codex-agent", "account_type": "agent",
            "agent_kind": "codex", "monthly_price": 10,
        })
        self.assertEqual(self._run_main(), 0)
        self.assertEqual(self._codex_rows(), 0)


if __name__ == "__main__":
    unittest.main()
