"""适配器根发现：CLINE_DIR 的 ``<dir>/data`` 派生（02-F-12）与本地重定位变量（02-F-13）。

02-F-13 是 info 级条目：本仓库比上游多支持 5 个重定位环境变量。这里锁定它们的
**行为**（设置后 discover 必须命中该目录），以免变量名或优先级被无意改掉。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.agent_usage.adapters import (
    cline, cola, copilot_cli, kimi_code, mimocode, qwen_code)
from app.tests.support import sqlite_connection


def _write(path: Path, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ClineDataRootTest(unittest.TestCase):
    def test_cline_dir_also_reads_its_data_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cline_dir = Path(directory) / "cline"
            data = cline_dir / "data"
            _write(data / "state" / "taskHistory.json", json.dumps([
                {"id": "task-1", "ulid": "task-1",
                 "cwdOnTaskInitialization": "/work/cline"},
            ]))
            _write(data / "tasks" / "task-1" / "ui_messages.json", json.dumps([
                {"ts": 1789916400000, "type": "say", "say": "api_req_started",
                 "text": json.dumps({"model": "cline-model", "tokensIn": 10,
                                     "tokensOut": 5, "cacheWrites": 2,
                                     "cacheReads": 3})},
            ]))
            with mock.patch.dict(os.environ, {
                    "CLINE_DIR": str(cline_dir),
                    "VIBE_USAGE_CLINE_DIRS": ""}):
                items = cline.discover({})
                # Parse inside the fixture's lifetime: the source file must
                # still exist when the adapter reads it.
                parsed = cline.parse(items[0])

        self.assertEqual(
            [item.path for item in items],
            [data / "tasks" / "task-1" / "ui_messages.json"])
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual((parsed.events[0].model, parsed.events[0].prompt_tokens,
                          parsed.events[0].cache_read_tokens),
                         ("cline-model", 15, 3))


class RelocationEnvironmentTest(unittest.TestCase):
    """02-F-13: each local-only relocation variable must still win."""

    def test_copilot_session_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "copilot"
            events = _write(root / "session-1" / "events.jsonl",
                            json.dumps({"type": "session.start"}))
            with mock.patch.dict(os.environ,
                                 {"COPILOT_SESSION_STATE_DIR": str(root)}):
                items = copilot_cli.discover({})
        self.assertEqual([item.path for item in items], [events])

    def test_qwen_tmp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "qwen"
            session = _write(root / "2026" / "chats" / "session.jsonl",
                             json.dumps({"type": "assistant"}))
            with mock.patch.dict(os.environ, {"QWEN_TMP_DIR": str(root)}):
                items = qwen_code.discover({})
        self.assertEqual([item.path for item in items], [session])

    def test_kimi_usage_legacy_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kimi"
            wire = _write(
                root / "sessions" / "wd" / "session" / "agents" / "main"
                / "wire.jsonl", "")
            with mock.patch.dict(os.environ, {
                    "KIMI_USAGE_LEGACY_ROOT": str(root)}):
                items = kimi_code.discover({})
        self.assertIn(wire, [item.path for item in items])

    def test_cola_sessions_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cola"
            session = _write(root / "session.jsonl",
                             json.dumps({"type": "session"}))
            with mock.patch.dict(os.environ,
                                 {"VIBE_USAGE_COLA_SESSIONS": str(root)}):
                items = cola.discover({})
        self.assertEqual([item.path for item in items], [session])

    def test_mimocode_db(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "mimocode.db"
            with sqlite_connection(database):
                pass
            with mock.patch.dict(os.environ,
                                 {"VIBE_USAGE_MIMOCODE_DB": str(database)}):
                items = mimocode.discover({})
        self.assertEqual([item.path for item in items], [database])


if __name__ == "__main__":
    unittest.main()
