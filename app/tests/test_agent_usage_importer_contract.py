"""导入器契约：解析缓存、锁失败可观测、stat 指纹含 inode、收据冲突后重算。

对应审计条目：

* 02-F-08：ALWAYS_SCAN 适配器每轮全量重解析。修复是 opt-in 的解析缓存
  （``module.PARSE_CACHE = True`` → ``AdapterSpec.parse_cache``），命中/未命中
  计数与可读日志可观测，且 skipped/读取失败/内容变化的批次不得被缓存复用。
* 02-F-10：`sqlite_rows` 在锁冲突时静默返回 ``[]``。修复保留适配器可见的返回
  值（导入不应因外部库失败而中止），但通过
  ``common.sqlite_read_failure_count()`` 计数 + warning 日志 + 导入器写入
  ``agent_software_runtime.last_error`` 让失败可见，并且不推进该来源游标。
* 02-F-16：两阶段 stat 只比较 size + mtime_ns。指纹必须含 inode，日志轮转产生
  “同名同大小同 mtime_ns”的新文件时必须重新解析。
* 02-F-11：收据已存在时只补写 duration_ms。同一 event_id 以新的 token 值再次
  写入时，token 列必须更新且成本必须重算（DB 级结果）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.services.agent_usage import common, importer
from app.services.agent_usage.adapters import alma, gemini_cli
from app.services.agent_usage.common import source
from app.services.agent_usage.ir import ParseBatch, UsageEvent, UsageSource
from app.services.agent_usage.skeleton import AdapterSpec
from app.tests.support import AppDatabaseTestCase, sqlite_connection


# --------------------------------------------------------------------------
# 02-F-08: parse cache
# --------------------------------------------------------------------------


class _CacheAdapter:
    """ALWAYS_SCAN adapter whose batch only depends on its source file."""

    KIND = "codex"

    def __init__(self, path: Path, *, skipped: bool = False,
                 event_count: int = 1) -> None:
        self.path = path
        self.skipped = skipped
        self.event_count = event_count
        self.parse_calls = 0

    def parse(self, item, stop_event=None, **_kwargs) -> ParseBatch:
        self.parse_calls += 1
        marker = item.path.read_text(encoding="utf-8").strip()
        event = UsageEvent(
            model="cache-model", prompt_tokens=10, completion_tokens=5,
            cache_read_tokens=0, total_tokens=15,
            requested_at="2026-08-13T01:00:00Z",
            event_id=f"cache:{marker}")
        events = [event] if self.event_count else []
        return ParseBatch.from_events(
            events, len(events), skipped=self.skipped,
            warnings=(("cache skipped",) if self.skipped else ()))

    def spec(self, *, parse_cache: bool) -> AdapterSpec:
        def discover(software, stop_event=None):
            return [UsageSource(path=self.path, key=str(self.path))]

        return AdapterSpec(kind=self.KIND, discover=discover,
                           parse=self.parse, always_scan=True,
                           parse_cache=parse_cache)


class ParseCacheTest(AppDatabaseTestCase):
    def _require_cache(self):
        cache = getattr(importer, "parse_cache", None)
        if cache is None or not hasattr(cache, "stats"):
            self.fail("02-F-08: importer.parse_cache 不存在，ALWAYS_SCAN 源仍每轮全量重解析")
        cache.reset()
        return cache

    def _software(self) -> int:
        return self.proxy_database().create_agent_software({
            "name": "parse-cache-agent", "agent_kind": "codex", "config": {},
        })

    def _import(self, database, adapter, spec) -> int:
        original = (importer.get_adapter, importer.get_adapter_spec)
        importer.get_adapter = lambda kind: adapter
        importer.get_adapter_spec = lambda kind: spec
        try:
            return importer._import_software(
                database, {"id": self._software_id, "agent_kind": "codex"},
                None)
        finally:
            importer.get_adapter, importer.get_adapter_spec = original

    def setUp(self) -> None:
        super().setUp()
        self._software_id = self._software()
        self._temporary_sources = tempfile.TemporaryDirectory()
        self.source_path = Path(self._temporary_sources.name) / "session.log"
        self.source_path.write_text("first\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary_sources.cleanup()
        super().tearDown()

    def test_unchanged_source_is_cached_and_counted(self) -> None:
        cache = self._require_cache()
        database = self.proxy_database()
        adapter = _CacheAdapter(self.source_path)
        spec = adapter.spec(parse_cache=True)
        with self.assertLogs("app.services.agent_usage.importer",
                             level="INFO") as captured:
            first = self._import(database, adapter, spec)
            second = self._import(database, adapter, spec)
        stats = cache.stats()
        self.assertEqual(adapter.parse_calls, 1,
                         "an unchanged ALWAYS_SCAN source was parsed twice")
        self.assertGreaterEqual(stats["hits"], 1, "cache hits are not counted")
        self.assertEqual((first, second), (1, 0),
                         "replaying the cached batch changed the imported rows")
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any("parse cache" in line and "hits=1" in line
                            for line in messages), messages)

    def test_adapters_without_opt_in_are_never_cached(self) -> None:
        cache = self._require_cache()
        database = self.proxy_database()
        adapter = _CacheAdapter(self.source_path)
        spec = adapter.spec(parse_cache=False)
        self._import(database, adapter, spec)
        self._import(database, adapter, spec)
        self.assertEqual(adapter.parse_calls, 2,
                         "a source that did not opt in was replayed from cache")
        self.assertEqual(cache.stats()["hits"], 0)

    def test_changed_source_is_reparsed(self) -> None:
        cache = self._require_cache()
        database = self.proxy_database()
        adapter = _CacheAdapter(self.source_path)
        spec = adapter.spec(parse_cache=True)
        self._import(database, adapter, spec)
        self.source_path.write_text("second\n", encoding="utf-8")
        inserted = self._import(database, adapter, spec)
        self.assertEqual(adapter.parse_calls, 2)
        self.assertEqual(inserted, 1)
        self.assertEqual(cache.stats()["hits"], 0)

    def test_skipped_batch_is_not_cached(self) -> None:
        self._require_cache()
        database = self.proxy_database()
        adapter = _CacheAdapter(self.source_path, skipped=True)
        spec = adapter.spec(parse_cache=True)
        self._import(database, adapter, spec)
        self._import(database, adapter, spec)
        self.assertEqual(adapter.parse_calls, 2,
                         "a skipped batch was cached and replayed next pass")

    def test_empty_batch_is_not_cached(self) -> None:
        self._require_cache()
        database = self.proxy_database()
        adapter = _CacheAdapter(self.source_path, event_count=0)
        spec = adapter.spec(parse_cache=True)
        self._import(database, adapter, spec)
        self._import(database, adapter, spec)
        self.assertEqual(adapter.parse_calls, 2,
                         "an empty batch was cached and replayed next pass")


# --------------------------------------------------------------------------
# 02-F-10: failed external SQLite read must be observable
# --------------------------------------------------------------------------


class SqliteReadFailureVisibilityTest(unittest.TestCase):
    def test_failed_read_is_counted_and_logged(self) -> None:
        counter = getattr(common, "sqlite_read_failure_count", None)
        if counter is None:
            self.fail("02-F-10: common.sqlite_read_failure_count() 不存在，"
                      "外部库读取失败仍静默返回 []")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_threads.db"
            with sqlite_connection(path):
                pass
            before = counter()
            with mock.patch.object(
                    common.sqlite_runtime, "read_only",
                    side_effect=sqlite3.OperationalError("database is locked")):
                with self.assertLogs("app.services.agent_usage.common",
                                     level="WARNING") as captured:
                    parsed = alma.parse(source(path))
            after = counter()
        self.assertEqual(parsed.events, ())
        self.assertEqual(after, before + 1)
        message = " ".join(record.getMessage() for record in captured.records)
        self.assertIn("database is locked", message)
        self.assertIn(str(path), message)


class MissingSourceIsNotAReadFailureTest(unittest.TestCase):
    def test_missing_database_file_is_not_counted_as_a_failure(self) -> None:
        counter = getattr(common, "sqlite_read_failure_count", None)
        if counter is None:
            self.fail("02-F-10: common.sqlite_read_failure_count() 不存在")
        before = counter()
        missing = Path("/nonexistent/definitely-absent-agent.db")
        with mock.patch.object(
                common.sqlite_runtime, "read_only",
                side_effect=AssertionError("a missing file must not be opened")):
            parsed = alma.parse(source(missing))
        self.assertEqual(parsed.events, ())
        self.assertEqual(counter(), before,
                         "a missing agent database was counted as a read failure")


class WorkerHealthTest(unittest.TestCase):
    def test_failed_read_marks_the_import_worker_degraded(self) -> None:
        counter = getattr(common, "sqlite_read_failure_count", None)
        if counter is None:
            self.fail("02-F-10: common.sqlite_read_failure_count() 不存在")
        from app.services import runtime_tasks
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat_threads.db"
            with sqlite_connection(path):
                pass

            def failing_action() -> int:
                with mock.patch.object(
                        common.sqlite_runtime, "read_only",
                        side_effect=sqlite3.OperationalError("database is locked")):
                    common.sqlite_rows(path, "SELECT 1")
                return 0

            health: dict = {}
            worker = runtime_tasks.AgentUsageImportWorker(
                failing_action, interval=1800, health=health,
                health_lock=threading.Lock())
            worker._run_once()
            degraded = dict(health["agent-usage-importer"])
            worker._action = lambda: 0
            worker._run_once()
            recovered = dict(health["agent-usage-importer"])

        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["sqlite_read_failures"], 1)
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["sqlite_read_failures"], 0)


class SqliteReadFailureImportTest(AppDatabaseTestCase):
    def test_failed_read_reaches_last_error_and_keeps_the_cursor(self) -> None:
        counter = getattr(common, "sqlite_read_failure_count", None)
        if counter is None:
            self.fail("02-F-10: common.sqlite_read_failure_count() 不存在")
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chat_threads.db"
            with sqlite_connection(database_path):
                pass
            database = self.proxy_database()
            database.create_agent_software({
                "name": "alma-read-failure", "agent_kind": "alma",
                "config": {"data_root": str(database_path)},
            })
            before = counter()
            with mock.patch.object(
                    common.sqlite_runtime, "read_only",
                    side_effect=sqlite3.OperationalError("database is locked")):
                inserted = importer.import_once(database)

        self.assertEqual(inserted, 0)
        self.assertGreater(counter(), before)
        with sqlite_connection(self.proxy_path) as conn:
            last_error, cursor = conn.execute(
                "SELECT r.last_error,r.cursor_json FROM agent_software s "
                "JOIN agent_software_runtime r ON r.software_id=s.id "
                "WHERE s.agent_kind='alma'").fetchone()
            states = json.loads(cursor) if cursor else {}
            self.assertEqual(states, {},
                             "a failed read advanced the source cursor")
        self.assertTrue(last_error)
        self.assertIn("SQLite", last_error)


# --------------------------------------------------------------------------
# 02-F-16: stat fingerprint includes inode
# --------------------------------------------------------------------------


class StatSignatureInodeTest(AppDatabaseTestCase):
    def test_new_inode_with_same_size_and_mtime_is_reparsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.json"
            events = {
                "directories": ["/work/inode"],
                "messages": [
                    {"role": "user", "timestamp": "2026-09-20T15:00:00Z"},
                    {"type": "gemini", "model": "gemini-inode",
                     "createTime": "2026-09-20T15:01:00Z",
                     "usageMetadata": {"promptTokenCount": 100,
                                       "candidatesTokenCount": 1}},
                ],
            }
            path.write_text(json.dumps(events), encoding="utf-8")
            before = path.stat()

            database = self.proxy_database()
            database.create_agent_software({
                "name": "gemini-inode", "agent_kind": "gemini-cli",
                "config": {"data_root": str(root)},
            })

            calls = []
            real_parse = gemini_cli.parse

            def counting_parse(item, *args, **kwargs):
                calls.append(str(item.path))
                return real_parse(item, *args, **kwargs)

            with mock.patch.object(gemini_cli, "parse", counting_parse):
                self.assertEqual(importer.import_once(database), 1)
                # Replace the log with a different file of identical size and
                # mtime (a rotation that keeps the timestamp).  The inode is
                # necessarily different because both files exist side by side.
                replacement = root / "rotated.json"
                rotated = {
                    "directories": ["/work/inode"],
                    "messages": [
                        {"role": "user", "timestamp": "2026-09-20T15:00:00Z"},
                        {"type": "gemini", "model": "gemini-inode",
                         "createTime": "2026-09-20T15:01:00Z",
                         "usageMetadata": {"promptTokenCount": 200,
                                           "candidatesTokenCount": 1}},
                    ],
                }
                replacement.write_text(json.dumps(rotated), encoding="utf-8")
                self.assertEqual(replacement.stat().st_size, before.st_size)
                os.utime(replacement, ns=(before.st_atime_ns,
                                          before.st_mtime_ns))
                os.replace(replacement, path)
                self.assertNotEqual(path.stat().st_ino, before.st_ino)
                importer.import_once(database)

        self.assertEqual(len(calls), 2,
                         "a same-size/same-mtime file with a new inode was "
                         "treated as unchanged")


class StatIdentityUnitTest(unittest.TestCase):
    def _identity(self):
        identity = getattr(common, "stat_identity", None)
        if identity is None:
            self.fail("02-F-16: common.stat_identity 不存在（指纹仍只有 size+mtime）")
        return identity

    def test_identity_covers_inode_and_rejects_a_different_file(self) -> None:
        identity = self._identity()
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first"
            second = Path(directory) / "second"
            first.write_text("x", encoding="utf-8")
            second.write_text("x", encoding="utf-8")
            one = identity(first.stat())
            two = identity(second.stat())
            self.assertIn("ino", one)
            self.assertIn("dev", one)
            self.assertNotEqual(one["ino"], two["ino"])
            self.assertFalse(importer._unchanged(first.stat(), second.stat()),
                             "two different inodes were reported as one file")
            self.assertTrue(importer._unchanged(first.stat(), first.stat()))

    def test_legacy_cursor_without_inode_is_reparsed_once(self) -> None:
        self._identity()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.log"
            path.write_text("x", encoding="utf-8")
            stat = path.stat()
            legacy = {"size": stat.st_size,
                      "mtime": int(stat.st_mtime),
                      "mtime_ns": int(stat.st_mtime_ns)}
            self.assertFalse(
                importer._same_stat(legacy, stat),
                "a cursor written before inodes is treated as unchanged")


# --------------------------------------------------------------------------
# 02-F-11: a conflicting receipt updates tokens and reprices
# --------------------------------------------------------------------------


class ReceiptRecomputeTest(AppDatabaseTestCase):
    """02-F-11: a re-import that repairs tokens must re-price the row.

    The permanent receipt blocks a second INSERT, so the repair path itself has
    to rewrite the token columns and reset the row to pending; the V2.12
    AFTER UPDATE trigger is then the only pricing authority that may compute the
    new amount (the AFTER INSERT trigger cannot fire for an UPDATE).  Both
    halves are asserted, plus the no-op path when the tokens agree.
    """

    def _fixture(self):
        database = self.proxy_database()
        software_id = database.create_agent_software({
            "name": "reprice-agent", "agent_kind": "gemini-cli",
            "config": {},
        })
        database.create_pricing({
            "model_pattern": "reprice-model",
            "input_price": 10.0,
            "cache_read_price": 0.0,
            "output_price": 1.0,
        })
        return database, software_id

    def _insert(self, database, software_id, prompt_tokens: int,
                event_id: str = "reprice-event") -> int:
        """Insert the same event and return the number of rows SQLite changed."""
        with sqlite_connection(self.proxy_path) as conn:
            before = conn.total_changes
            database._insert_agent_usage_row(
                conn, software_id, "reprice-model", prompt_tokens, 0, 0,
                prompt_tokens, "2025-06-01T12:00:00Z", event_id)
            return conn.total_changes - before

    def _row(self, event_id: str = "reprice-event") -> tuple:
        with sqlite_connection(self.proxy_path) as conn:
            return tuple(conn.execute(
                "SELECT prompt_tokens,total_tokens,pricing_status,"
                "equivalent_cost FROM request_log "
                "WHERE event_id=?", (event_id,)).fetchone())

    def test_repair_rewrites_tokens_and_reprices_through_the_trigger(self) -> None:
        database, software_id = self._fixture()
        self._insert(database, software_id, 1_000)
        self.assertEqual(self._row()[:4], (1_000, 1_000, "rated", 0.01))

        # The adapter now reports the same event with 1000x more tokens (the
        # reply was still streaming on the first pass).  The receipt exists,
        # so the repair path must move the row and V2.12 must re-price it.
        self._insert(database, software_id, 1_000_000)
        self.assertEqual(self._row()[:4], (1_000_000, 1_000_000, "rated", 10.0))

        # A third identical pass writes nothing at all (the repair UPDATE is
        # guarded by a token difference), so no re-pricing loop can occur.
        self.assertEqual(self._insert(database, software_id, 1_000_000), 0)
        self.assertEqual(self._row()[:4], (1_000_000, 1_000_000, "rated", 10.0))
        with sqlite_connection(self.proxy_path) as conn:
            receipts = conn.execute(
                "SELECT count(*) FROM agent_usage_receipts "
                "WHERE event_id='reprice-event'").fetchone()[0]
        self.assertEqual(receipts, 1)

    def test_repair_without_the_update_trigger_keeps_the_row_pending(self) -> None:
        database, software_id = self._fixture()
        self._insert(database, software_id, 1_000, event_id="no-trigger")
        self.assertEqual(self._row("no-trigger")[:4],
                         (1_000, 1_000, "rated", 0.01))
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute("DROP TRIGGER IF EXISTS reprice_usage_event")
        self._insert(database, software_id, 1_000_000, event_id="no-trigger")
        # Without the V2.12 AFTER UPDATE trigger the repaired row is left
        # pending at 0.0: this is why the migration and the routing change are
        # both required for 02-F-11.
        self.assertEqual(self._row("no-trigger")[:4],
                         (1_000_000, 1_000_000, "pending", 0.0))


if __name__ == "__main__":
    unittest.main()
