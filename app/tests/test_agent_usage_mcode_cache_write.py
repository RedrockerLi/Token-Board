"""mcode 写缓存 token 的入账口径（02-F-17 回归锁定）。

审计报告 02-F-17 断言 `mcode` 从 SQL 取出 `cache_write_tokens` 后整段丢弃。
当前实现的账本行事件把 `cache_write_tokens` 并入 `input_tokens`
（`adapters/mcode.py` 的 `input_tokens = _number(row[3]) + _number(row[7])`），
`UsageEvent.from_buckets` 再把 input 与 cache-read 相加为 prompt。

现有 ``test_mcode_reads_allowlisted_runtime_usage`` 的夹具同时带 cache-read 与
cache-write，无法区分两者。本用例把 cache-read 置 0，只让写缓存影响 prompt，
这样任何一次“写缓存不入账”的退化都会立刻失败。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.agent_usage.adapters import mcode
from app.tests.support import sqlite_connection


class McodeCacheWriteAccountingTest(unittest.TestCase):
    def test_cache_write_tokens_are_prompt_tokens_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-state.sqlite"
            with sqlite_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE local_runtime_sessions ("
                    "session_id TEXT PRIMARY KEY, workspace_dir TEXT,"
                    "project_workspace_dir TEXT)")
                connection.execute(
                    "CREATE TABLE local_runtime_token_usage ("
                    "session_id TEXT, model TEXT, ts INTEGER,"
                    "input_tokens INTEGER, output_tokens INTEGER,"
                    "reasoning_tokens INTEGER, cache_read_tokens INTEGER,"
                    "cache_write_tokens INTEGER, raw TEXT)")
                connection.execute(
                    "INSERT INTO local_runtime_sessions VALUES(?,?,?)",
                    ("session-1", "/tmp/workspace", "/work/cache-write"))
                # input=3, output=4, reasoning=0, cache_read=0, cache_write=7
                connection.execute(
                    "INSERT INTO local_runtime_token_usage "
                    "VALUES(?,?,?,?,?,?,?,?,NULL)",
                    ("session-1", "mcode-model", 1782720000000, 3, 4, 0, 0, 7))

            items = mcode.discover({"config": {"data_root": str(path)}})
            self.assertEqual(len(items), 1)
            parsed = mcode.parse(items[0])
            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            # prompt = input + cache_write (+ cache_read = 0), completion = output.
            self.assertEqual(event.prompt_tokens, 10)
            self.assertEqual(event.completion_tokens, 4)
            self.assertEqual(event.cache_read_tokens, 0)
            self.assertEqual(event.total_tokens, 14)


if __name__ == "__main__":
    unittest.main()
