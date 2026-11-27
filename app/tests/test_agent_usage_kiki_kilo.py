"""02-F-01：本地注册表与上游 HEAD 的 36 个来源一一对应，kiki / kilo 可解析。

注册计数与集合由 ``test_agent_usage_adapters.test_registry_matches_reference_agent_set``
守护；本文件只补这两个新增 kind 的**数值**行为（token / 事件数），避免“注册了但
解析不出用量”的空转覆盖。

夹具依据适配器读取的物理布局自造：

* kiki 复用 Kimi 线协议 ``sessions/<work>/<session>/agents/<agent>/wire.jsonl``，
  并把已知路由前缀从 model id 上剥掉；
* kilo 读 XDG 数据目录里的 ``kilo.db``（session/message/part 三表 + JSON data）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.agent_usage.registry import get_adapter
from app.tests.support import sqlite_connection


def _adapter(kind: str):
    adapter = get_adapter(kind)
    if adapter is None:
        raise AssertionError(
            f"02-F-01: registry 缺少 {kind} 适配器（上游 HEAD 已有该来源）")
    return adapter


class KikiAdapterTest(unittest.TestCase):
    def _wire(self, root: Path) -> Path:
        wire = (root / "sessions" / "wd_project" / "session-1" / "agents"
                / "main" / "wire.jsonl")
        wire.parent.mkdir(parents=True)
        records = [
            {"type": "usage.record", "id": "r1",
             "model": "kimi-code/kimi-k2", "time": 1782720000000,
             "usage": {"inputOther": 100, "inputCacheCreation": 10,
                       "inputCacheRead": 20, "output": 5}},
            {"type": "usage.record", "id": "r2",
             "model": "Qwen/Qwen3.8-Flash", "time": 1782720001000,
             "usage": {"inputOther": 50, "output": 2}},
        ]
        wire.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                        encoding="utf-8")
        return wire

    def test_kiki_parses_wire_usage_and_strips_routing_prefixes(self) -> None:
        adapter = _adapter("kiki")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "kiki"
            wire = self._wire(root)
            other = Path(directory) / "kimi-code"
            other.mkdir()
            with mock.patch.dict(os.environ, {
                    "VIBE_USAGE_KIKI_DIR": str(root),
                    "VIBE_USAGE_KIMI_CODE_DIR": str(other),
                    "VIBE_USAGE_KIMI_DIR": str(other),
                    "KIMI_CODE_HOME": "",
                    "KIMI_USAGE_LEGACY_ROOT": ""}):
                sources = adapter.discover({})
            self.assertEqual([item.path for item in sources], [wire])
            parsed = adapter.parse(sources[0])

        self.assertFalse(parsed.skipped, parsed.warnings)
        self.assertEqual(len(parsed.events), 2)
        first, second = parsed.events
        self.assertEqual(first.model, "kimi-k2")
        # prompt = inputOther(100) + inputCacheCreation(10) + inputCacheRead(20)
        self.assertEqual((first.prompt_tokens, first.completion_tokens,
                          first.cache_read_tokens, first.total_tokens),
                         (130, 5, 20, 135))
        # A slash-bearing concrete model id must not be mistaken for a prefix.
        self.assertEqual(second.model, "Qwen/Qwen3.8-Flash")
        self.assertEqual((second.prompt_tokens, second.completion_tokens,
                          second.total_tokens), (50, 2, 52))


class KiloAdapterTest(unittest.TestCase):
    def _kilo_db(self, root: Path) -> Path:
        path = root / "kilo.db"
        root.mkdir(parents=True)
        with sqlite_connection(path) as conn:
            conn.executescript("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
                    time_created INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY, session_id TEXT,
                    time_created INTEGER, data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY, session_id TEXT, message_id TEXT,
                    time_created INTEGER, data TEXT
                );
            """)
            conn.execute(
                "INSERT INTO session VALUES('s1',NULL,'/work/kilo',1782719999000)")
            conn.execute(
                "INSERT INTO message VALUES('m1','s1',1782720000000,?)",
                (json.dumps({
                    "role": "assistant", "modelID": "kilo-model",
                    "path": {"cwd": "/work/kilo"},
                    "time": {"created": 1782720000000},
                    "tokens": {"input": 100, "output": 20, "reasoning": 3,
                               "cache": {"read": 5, "write": 7}},
                }),))
        return path

    def test_kilo_parses_sqlite_message_usage(self) -> None:
        adapter = _adapter("kilo")
        with tempfile.TemporaryDirectory() as directory:
            database = self._kilo_db(Path(directory) / "kilo")
            sources = adapter.discover({"config": {"data_root": str(database)}})
            self.assertEqual([item.path for item in sources],
                             [database.resolve()])
            parsed = adapter.parse(sources[0])

        self.assertFalse(parsed.skipped, parsed.warnings)
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        self.assertEqual(event.model, "kilo-model")
        self.assertEqual(event.project, "kilo")
        # prompt = input(100) + cache_write(7) + cache_read(5);
        # completion = output(20) + reasoning(3).
        self.assertEqual((event.prompt_tokens, event.completion_tokens,
                          event.cache_read_tokens, event.total_tokens),
                         (112, 23, 5, 135))


if __name__ == "__main__":
    unittest.main()
