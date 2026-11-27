"""DSH 压缩会话解码与事件身份的回归测试。

覆盖两条审计条目：

* 02-F-03：DSH 的 ``.jsonl.zstd`` 必须由解码库读取，不依赖外部 ``zstd`` CLI；
  任何解码库都不可用时必须 ``skipped=True`` + warning（可观测，不静默），
  帧头声明的内容长度与实际解压长度不一致时必须报错而不是导入截断内容。
* 02-F-15：记录没有 ``seq`` 但消息带 ``id`` 时，event_id 必须与消息在文件中的
  位置无关（当前实现会退回列表下标）。

压缩帧由测试用 RFC 8878 的 raw block 手工拼出（单段帧），因此用例既不依赖
外部编码器，也不需要预生成 base64 常量。
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.agent_usage.adapters import dsh, zstd_reader
from app.services.agent_usage.importer import import_once
from app.tests.support import AppDatabaseTestCase, sqlite_connection

ZSTD_MAGIC = 0xFD2FB528


def _frame(payload: bytes, *, declared_size: int | None = None) -> bytes:
    """Build one complete single-segment Zstandard frame with a raw block.

    ``declared_size`` rewrites Frame_Content_Size only; the block payload keeps
    its real length, so the frame stays parseable but internally inconsistent.
    """
    size = len(payload) if declared_size is None else declared_size
    if size < 256:
        descriptor = 0x20            # single segment, FCS flag 0 -> 1 byte
        size_field = bytes([size])
    else:
        descriptor = 0x60            # single segment, FCS flag 1 -> 2 bytes
        size_field = struct.pack("<H", size - 256)
    block_header = struct.pack("<I", 1 | (len(payload) << 3))[:3]
    return (struct.pack("<I", ZSTD_MAGIC) + bytes([descriptor]) + size_field
            + block_header + payload)


def _session_records(*, session_id: str = "zstd-session",
                     messages: list[dict]) -> list[dict]:
    return [{"type": "session", "version": 0, "id": session_id,
             "cwd": "/work/zstd"}, *messages]


def _user_record(time: str = "2026-08-24T01:00:00Z", **extra) -> dict:
    record = {"type": "user/message", "time": time,
              "data": {"source": {"kind": "user"}, "id": "user-1"}}
    record.update(extra)
    return record


def _assistant_record(*, model: str = "dsh-zstd", message_id: str = "m-1",
                      time: str = "2026-08-24T01:00:01Z", value: int = 10,
                      **extra) -> dict:
    record = {
        "type": "assistant/message", "time": time,
        "data": {
            "message": {"source": {"model": model}, "id": message_id},
            "usage": {"inputTokens": value, "cacheWriteTokens": 2,
                      "cacheReadTokens": 3, "outputTokens": 10,
                      "reasoningTokens": 4},
        },
    }
    record.update(extra)
    return record


def _lines(records: list[dict]) -> bytes:
    return ("\n".join(json.dumps(record) for record in records)
            + "\n").encode("utf-8")


class DshZstdDecodingTest(unittest.TestCase):
    def _source(self, directory: str, records: list[dict], *,
                declared_size: int | None = None, name: str = "session.jsonl.zstd"):
        path = Path(directory) / name
        path.write_bytes(_frame(_lines(records), declared_size=declared_size))
        return dsh.discover({"config": {"data_root": str(path)}})[0]

    def test_compressed_session_decodes_without_external_zstd_binary(self) -> None:
        records = _session_records(messages=[_user_record(),
                                             _assistant_record()])
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, records)
            with mock.patch.dict(os.environ, {"PATH": ""}), \
                    mock.patch("shutil.which", return_value=None):
                parsed = dsh.parse(source)
        self.assertFalse(parsed.skipped, parsed.warnings)
        self.assertEqual(parsed.warnings, ())
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        # prompt = input(10) + cache_write(2) + cache_read(3);
        # completion = output(10) = non-reasoning output(6) + reasoning(4).
        self.assertEqual(event.prompt_tokens, 15)
        self.assertEqual(event.completion_tokens, 10)
        self.assertEqual(event.cache_read_tokens, 3)

    def test_missing_decoder_library_is_skipped_with_a_warning(self) -> None:
        records = _session_records(messages=[_user_record(),
                                             _assistant_record()])
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, records)
            with mock.patch.object(zstd_reader, "_import_backend",
                                   return_value=None), \
                    mock.patch.dict(os.environ, {"PATH": ""}):
                parsed = dsh.parse(source)
        self.assertTrue(parsed.skipped)
        self.assertEqual(parsed.events, ())
        message = " ".join(parsed.warnings)
        self.assertIn("zstandard", message.lower())
        self.assertIn(str(source.path), message)

    def test_frame_content_size_mismatch_is_skipped_with_a_warning(self) -> None:
        records = _session_records(messages=[_user_record(),
                                             _assistant_record()])
        payload = _lines(records)
        with tempfile.TemporaryDirectory() as directory:
            source = self._source(directory, records,
                                  declared_size=len(payload) - 1)
            with mock.patch.dict(os.environ, {"PATH": ""}):
                parsed = dsh.parse(source)
        self.assertTrue(parsed.skipped)
        self.assertEqual(parsed.events, ())
        self.assertTrue(parsed.warnings)

    def test_torn_trailing_frame_keeps_the_complete_prefix(self) -> None:
        first = _frame(_lines(_session_records(
            messages=[_user_record(), _assistant_record(message_id="m-1")])))
        second = _frame(_lines([_assistant_record(message_id="m-2",
                                                  time="2026-08-24T01:00:02Z")]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl.zstd"
            # A live session keeps appending: the last frame is torn mid-block.
            path.write_bytes(first + second[:10])
            source = dsh.discover({"config": {"data_root": str(path)}})[0]
            with mock.patch.dict(os.environ, {"PATH": ""}):
                parsed = dsh.parse(source)
        self.assertFalse(parsed.skipped, parsed.warnings)
        self.assertEqual(len(parsed.events), 1)
        # Only the complete first frame is imported; the torn tail is ignored.
        self.assertEqual(parsed.events[0].requested_at, "2026-08-24T01:00:01Z")


class DshMessageIdentityTest(unittest.TestCase):
    """02-F-15: a message id must survive a change of its file position."""

    def _event_ids(self, directory: str, records: list[dict]) -> set[str]:
        path = Path(directory) / "session.jsonl"
        path.write_text(_lines(records).decode("utf-8"), encoding="utf-8")
        source = dsh.discover({"config": {"data_root": str(path)}})[0]
        parsed = dsh.parse(source)
        self.assertFalse(parsed.skipped, parsed.warnings)
        return {event.event_id for event in parsed.events}

    def test_event_id_is_stable_when_messages_have_ids_but_no_seq(self) -> None:
        assistant = _assistant_record(message_id="m-1")
        user = _user_record()
        with tempfile.TemporaryDirectory() as directory:
            ids_before = self._event_ids(
                directory, _session_records(messages=[user, assistant]))
            # Same session, same messages, different physical order: without a
            # seq the current implementation falls back to the list index.
            ids_after = self._event_ids(
                directory, _session_records(messages=[assistant, user]))
        self.assertEqual(ids_before, ids_after)


if __name__ == "__main__":
    unittest.main()


class DshForkReplayTest(AppDatabaseTestCase):
    """O-12: a v2+ fork without an inherited prefix must not break the pass.

    ``_build_model`` reports ``seed_length=None`` for a forked session that was
    not seeded (``isSeeded`` false, no inherited end-seed marker).  The replay
    helper compared it with ``<= 0``, so ``replay_skips`` raised TypeError — and
    the importer calls that *before* its own try block, which took the whole
    import pass down and left every kind unimported.  A genuine seeded prefix
    must still be skipped: treating it as zero would import the inherited
    history a second time.
    """

    @staticmethod
    def _user(seq: int, message_id: str) -> dict:
        return {"seq": seq, "type": "user/message", "time": "2026-08-24T01:00:00Z",
                "data": {"id": message_id, "source": {"kind": "user"}}}

    @staticmethod
    def _assistant(seq: int, message_id: str, value: int) -> dict:
        return {"seq": seq, "type": "assistant/message",
                "time": f"2026-08-24T01:00:0{seq}Z",
                "data": {"message": {"id": message_id,
                                     "source": {"model": "dsh-model"}},
                         "usage": {"inputTokens": value, "outputTokens": 10}}}

    def _write(self, root: Path, name: str, records: list[dict]) -> Path:
        directory = root / "sessions" / "proj" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "session.v4.jsonl").write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8")
        return directory

    def _software(self, name: str, kind: str, root: Path) -> int:
        return self.proxy_database().create_agent_software({
            "name": name, "agent_kind": kind,
            "config": {"data_root": str(root)},
        })

    def test_unseeded_fork_does_not_abort_the_import_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dsh_root = root / "dsh"
            self._write(dsh_root, "parent", [
                {"type": "session", "version": 4, "id": "parent",
                 "isSeeded": False},
                self._user(1, "u-1"), self._assistant(2, "a-1", 10),
            ])
            self._write(dsh_root, "fork", [
                {"type": "session", "version": 4, "id": "fork",
                 "isSeeded": False, "parentSession": "parent", "forked": True},
                self._user(1, "u-1"), self._assistant(2, "a-1", 10),
            ])
            openclaw_root = root / "openclaw"
            session = openclaw_root / "agents" / "a1" / "sessions" / "run.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(json.dumps({
                "type": "message", "timestamp": "2026-09-21T14:13:20Z",
                "message": {"role": "assistant", "model": "claw-a",
                            "usage": {"input": 10, "output": 2}},
            }) + "\n", encoding="utf-8")
            database = self.proxy_database()
            self._software("dsh-fork", "dsh", dsh_root)
            self._software("openclaw-side", "openclaw", openclaw_root)

            # The defect escaped from replay_skips out of import_once, so the
            # call itself is the regression assertion: it must not raise.
            import_once(database)

            with sqlite_connection(self.proxy_path) as conn:
                rows = [tuple(row) for row in conn.execute(
                    "SELECT model,prompt_tokens FROM request_log ORDER BY id")]

        self.assertEqual(len(rows), 3, rows)
        # Parent + fork (one event each) and the side kind all reached the DB.
        self.assertEqual(sorted(rows),
                         [("claw-a", 10), ("dsh-model", 10), ("dsh-model", 10)])

    def test_seeded_fork_still_skips_its_inherited_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dsh_root = root / "dsh"
            self._write(dsh_root, "parent", [
                {"type": "session", "version": 4, "id": "parent",
                 "isSeeded": False},
                self._user(1, "u-1"), self._assistant(2, "a-1", 10),
            ])
            self._write(dsh_root, "child", [
                {"type": "session", "version": 4, "id": "child",
                 "isSeeded": True, "parentSession": "parent"},
                self._user(1, "u-1"), self._assistant(2, "a-1", 10),
                # The marker's seq is the boundary: messages with a smaller
                # seq are the inherited prefix (seq 1 and 2 here).
                {"type": "session/end-seed", "seq": 3,
                 "data": {"inherited": True}},
                self._user(3, "u-2"), self._assistant(4, "a-2", 20),
            ])
            sources = dsh.discover({"config": {"data_root": str(dsh_root)}})
            child = next(item for item in sources
                         if item.path.parent.name == "child")
            # The marker still defines a provable prefix of two messages.
            self.assertEqual(dsh.replay_skips(sources)[str(child.path)], 2)

            database = self.proxy_database()
            self._software("dsh-seeded", "dsh", dsh_root)
            import_once(database)
            with sqlite_connection(self.proxy_path) as conn:
                rows = [tuple(row) for row in conn.execute(
                    "SELECT prompt_tokens FROM request_log ORDER BY id")]

        # Parent's a-1 plus the child's own a-2 only: the inherited a-1 copy is
        # skipped once, never imported a second time.
        self.assertEqual(sorted(row[0] for row in rows), [10, 20])
