"""02-F-09: content-stable event identities for rewritten sources.

Seven adapters used the record's *position* as its identity (CSV row index,
JSONL line number, JSON array index, ``len(events)`` fallback).  Rewriting or
reordering a source handed already-imported usage a new ``event_id``; the
permanent receipt no longer matched and the same usage was counted twice.

Each case below is end-to-end through ``import_once`` and asserts the contract
from the audit:

* the same record set in a different physical order keeps every record's
  existing ``event_id`` (a reorder must not hand one record's id to another);
* appending one new record imports exactly one new row while the old rows keep
  their ids;
* identical records are told apart by an occurrence counter, and a record
  without usage never shifts another id.

The one-time re-key of receipts written under the old positional ids is a
separate, idempotent migration (w3 t46); these tests deliberately do not
tolerate duplicates.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_usage.adapters import antigravity, cursor
from app.services.agent_usage.importer import import_once
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class RecordIdentityUnitTest(unittest.TestCase):
    """The shared ordinal assignment (w7's ``record_identity``)."""

    def _module(self):
        try:
            from app.services.agent_usage.adapters import record_identity
        except ImportError:
            self.fail("02-F-09: adapters.record_identity 不存在")
        return record_identity

    @staticmethod
    def _payload(model: str, prompt: int, completion: int, cached: int = 0,
                 at: str = "2026-09-21T14:13:20Z") -> dict:
        return {"input_tokens": prompt, "output_tokens": completion,
                "cached_input_tokens": cached, "requested_at": at,
                "model": model}

    def test_ordinals_depend_on_the_multiset_not_the_order(self) -> None:
        module = self._module()
        payloads = [
            self._payload("m-a", 10, 2),
            self._payload("m-b", 20, 3),
            self._payload("m-a", 10, 2, at="2026-09-21T14:14:20Z"),
        ]
        forward = module.assign_ordinals(payloads)
        backward = module.assign_ordinals(list(reversed(payloads)))
        self.assertEqual(list(reversed(backward)), forward)
        self.assertEqual(len(set(forward)), 3)

    def test_identical_records_get_distinct_occurrences(self) -> None:
        module = self._module()
        one = self._payload("m-a", 10, 2)
        ordinals = module.assign_ordinals([dict(one), dict(one)])
        self.assertEqual(sorted(ordinals), sorted([f"{ordinals[0].split(':')[0]}:0",
                                                   f"{ordinals[0].split(':')[0]}:1"]))
        reversed_ordinals = module.assign_ordinals([dict(one), dict(one)])
        self.assertEqual(set(ordinals), set(reversed_ordinals))

    def test_zero_usage_records_take_no_ordinal(self) -> None:
        module = self._module()
        payloads = [self._payload("m-a", 0, 0),
                    self._payload("m-b", 20, 3)]
        ordinals = module.assign_ordinals(payloads)
        self.assertIsNone(ordinals[0])
        self.assertIsNotNone(ordinals[1])


class _IdentityImportCase(AppDatabaseTestCase):
    """End-to-end helper: reorder + append must add exactly one row."""

    def _software(self, kind: str, config: dict):
        database = self.proxy_database()
        database.create_agent_software({
            "name": f"{kind}-identity", "agent_kind": kind, "config": config,
        })
        return database

    def _rows(self) -> dict:
        with sqlite_connection(self.proxy_path) as conn:
            rows = conn.execute(
                "SELECT event_id,model,prompt_tokens,completion_tokens,"
                "cache_read_tokens FROM request_log ORDER BY id").fetchall()
        mapping = {(model, prompt, completion, cached): event_id
                   for event_id, model, prompt, completion, cached in rows}
        self.assertEqual(len(mapping), len(rows), "duplicate payloads in fixture")
        return mapping

    def _verify_incremental(self, before: dict) -> None:
        after = self._rows()
        self.assertEqual(len(after), len(before) + 1,
                         "reorder + one appended record must add exactly one row")
        for payload, event_id in before.items():
            self.assertIn(payload, after, f"record {payload} disappeared")
            self.assertEqual(after[payload], event_id,
                             f"record {payload} changed its event id")
        self.assertEqual(len(set(after) - set(before)), 1)


class OpenClawIdentityTest(_IdentityImportCase):
    @staticmethod
    def _records(order_first: bool, with_new: bool) -> list[dict]:
        records = [
            {"type": "message", "timestamp": "2026-09-21T14:13:20Z",
             "message": {"role": "assistant", "model": "claw-a",
                         "usage": {"input": 10, "output": 2}}},
            {"type": "message", "timestamp": "2026-09-21T14:14:20Z",
             "message": {"role": "assistant", "model": "claw-b",
                         "usage": {"input": 20, "output": 3}}},
        ]
        if with_new:
            records.append(
                {"type": "message", "timestamp": "2026-09-21T14:15:20Z",
                 "message": {"role": "assistant", "model": "claw-c",
                             "usage": {"input": 30, "output": 4}}})
        return list(reversed(records)) if order_first else records

    def test_reordered_jsonl_keeps_ids_and_appends_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "agents" / "agent-1" / "run.jsonl"
            session.parent.mkdir(parents=True)
            database = self._software("openclaw", {"data_root": directory})

            def write(records):
                session.write_text(
                    "\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")

            write(self._records(order_first=False, with_new=False))
            import_once(database)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(self._records(order_first=True, with_new=True))
            import_once(database)
        self._verify_incremental(before)


class GeminiJsonIdentityTest(_IdentityImportCase):
    @staticmethod
    def _records(reverse: bool, with_new: bool) -> list[dict]:
        records = [
            {"type": "gemini", "model": "gemini-a",
             "timestamp": "2026-09-21T14:13:20Z",
             "usageMetadata": {"promptTokenCount": 10, "cachedContentTokenCount": 3,
                               "candidatesTokenCount": 2}},
            {"type": "gemini", "model": "gemini-b",
             "timestamp": "2026-09-21T14:14:20Z",
             "usageMetadata": {"promptTokenCount": 20, "cachedContentTokenCount": 0,
                               "candidatesTokenCount": 4}},
        ]
        if with_new:
            records.append(
                {"type": "gemini", "model": "gemini-c",
                 "timestamp": "2026-09-21T14:15:20Z",
                 "usageMetadata": {"promptTokenCount": 30,
                                   "cachedContentTokenCount": 1,
                                   "candidatesTokenCount": 6}})
        return list(reversed(records)) if reverse else records

    def test_reordered_json_array_keeps_ids_and_appends_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            database = self._software("gemini-cli", {"data_root": directory})

            def write(records):
                path.write_text(json.dumps({"messages": records}),
                                encoding="utf-8")

            write(self._records(reverse=False, with_new=False))
            import_once(database)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(self._records(reverse=True, with_new=True))
            import_once(database)
        self._verify_incremental(before)


class AmpLedgerIdentityTest(_IdentityImportCase):
    @staticmethod
    def _thread(reverse: bool, with_new: bool) -> dict:
        events = [
            {"toMessageId": 0, "timestamp": "2026-09-21T14:13:20Z",
             "model": "amp-a", "tokens": {"input": 10, "output": 2}},
            {"toMessageId": 1, "timestamp": "2026-09-21T14:14:20Z",
             "model": "amp-b", "tokens": {"input": 20, "output": 3}},
        ]
        if with_new:
            events.append(
                {"toMessageId": 2, "timestamp": "2026-09-21T14:15:20Z",
                 "model": "amp-c", "tokens": {"input": 30, "output": 4}})
        return {"id": "thread-1",
                "messages": [{"usage": {"cacheReadInputTokens": 1}} for _ in events],
                "usageLedger": {"events": list(reversed(events)) if reverse else events}}

    def test_reordered_usage_ledger_keeps_ids_and_appends_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "T-1.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            database = self._software("amp", {"data_root": str(path.parent)})

            def write(thread):
                path.write_text(json.dumps(thread), encoding="utf-8")

            write(self._thread(reverse=False, with_new=False))
            import_once(database)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(self._thread(reverse=True, with_new=True))
            import_once(database)
        self._verify_incremental(before)


def _task_messages(reverse: bool, with_new: bool) -> list[dict]:
    messages = [
        {"type": "say", "say": "api_req_started", "ts": 1790000000000,
         "text": json.dumps({"model": "task-a", "tokensIn": 10, "tokensOut": 2,
                             "cacheWrites": 1, "cacheReads": 3})},
        {"type": "say", "say": "api_req_started", "ts": 1790000060000,
         "text": json.dumps({"model": "task-b", "tokensIn": 20, "tokensOut": 4,
                             "cacheWrites": 0, "cacheReads": 0})},
        {"type": "say", "say": "text", "ts": 1790000070000, "text": "no usage"},
    ]
    if with_new:
        messages.append(
            {"type": "say", "say": "api_req_started", "ts": 1790000120000,
             "text": json.dumps({"model": "task-c", "tokensIn": 30,
                                 "tokensOut": 6, "cacheWrites": 0,
                                 "cacheReads": 0})})
    return list(reversed(messages)) if reverse else messages


class ClineTaskIdentityTest(_IdentityImportCase):
    def test_rewritten_task_messages_keep_ids_and_append_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "state" / "taskHistory.json"
            history.parent.mkdir(parents=True)
            history.write_text(json.dumps([
                {"id": "task-1", "ulid": "task-1", "cwd": "/work/project"},
            ]), encoding="utf-8")
            messages_path = root / "tasks" / "task-1" / "ui_messages.json"
            messages_path.parent.mkdir(parents=True)
            database = self._software("cline", {"data_root": directory})

            def write(records):
                messages_path.write_text(json.dumps(records), encoding="utf-8")

            write(_task_messages(reverse=False, with_new=False))
            import_once(database)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(_task_messages(reverse=True, with_new=True))
            import_once(database)
        self._verify_incremental(before)


class RooCodeTaskIdentityTest(_IdentityImportCase):
    def test_rewritten_task_messages_keep_ids_and_append_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "_index.json").write_text(json.dumps({
                "entries": [{"id": "task-1", "workspace": "/work/project"}],
            }), encoding="utf-8")
            messages_path = tasks / "task-1" / "ui_messages.json"
            messages_path.parent.mkdir()
            database = self._software("roo-code", {"data_root": directory})

            def write(records):
                messages_path.write_text(json.dumps(records), encoding="utf-8")

            write(_task_messages(reverse=False, with_new=False))
            import_once(database)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(_task_messages(reverse=True, with_new=True))
            import_once(database)
        self._verify_incremental(before)


class CursorExportIdentityTest(_IdentityImportCase):
    HEADER = ("Date,Model,Input (w/ Cache Write),Input (w/o Cache Write),"
              "Cache Read,Output Tokens\n")
    ROWS = [
        "2026-09-21T14:13:20Z,cursor-model-a,10,5,3,7\n",
        "2026-09-21T14:14:20Z,cursor-model-b,20,0,0,2\n",
    ]
    NEW_ROW = "2026-09-21T14:15:20Z,cursor-model-c,30,0,0,4\n"

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self) -> bytes:
            return self._payload

    def test_reordered_csv_export_keeps_ids_and_appends_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.vscdb"
            with sqlite_connection(state) as conn:
                conn.execute("CREATE TABLE ItemTable(key TEXT, value TEXT)")
                conn.execute("INSERT INTO ItemTable VALUES"
                             "('cursorAuth/accessToken','token')")
            database = self._software("cursor", {"data_root": str(state)})

            def write(rows):
                payload = (self.HEADER + "".join(rows)).encode("utf-8")
                with patch.object(cursor, "urlopen",
                                  lambda *a, **k: self._Response(payload)):
                    import_once(database)

            write(self.ROWS)
            before = self._rows()
            self.assertEqual(len(before), 2)

            write(list(reversed(self.ROWS)) + [self.NEW_ROW])
        self._verify_incremental(before)


class AntigravityLegacyIdentityTest(_IdentityImportCase):
    @staticmethod
    def _trajectory(retries: list[dict]) -> dict:
        return {
            "metadata": {"workspaces": [
                {"repository": {"computedName": "proj-alpha"}}]},
            "generatorMetadata": [{
                "chatModel": {
                    "chatStartMetadata": {"createdAt": "2026-09-21T14:13:20Z"},
                    "retryInfos": retries,
                },
            }],
        }

    def test_anonymous_retries_keep_ids_and_append_one_row(self) -> None:
        retries = [
            {"usage": {"responseId": "RESP-1", "inputTokens": 10,
                       "outputTokens": 2}},
            {"usage": {"inputTokens": 20, "outputTokens": 3}},
            {"usage": {"inputTokens": 30, "outputTokens": 4}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            conversations = Path(directory) / "conversations"
            conversations.mkdir()
            (conversations / "session-1.pb").write_bytes(b"captured legacy log")
            database = self._software("antigravity",
                                      {"data_root": str(conversations)})

            def write(order: list[dict]):
                trajectory = self._trajectory(order)
                with patch.object(antigravity, "_legacy_trajectory",
                                  lambda stem: trajectory):
                    import_once(database)

            write(retries)
            before = self._rows()
            self.assertEqual(len(before), 3)

            appended = {"usage": {"inputTokens": 40, "outputTokens": 5}}
            write(list(reversed(retries)) + [appended])
            after = self._rows()
        self.assertEqual(len(after), len(before) + 1)
        for payload, event_id in before.items():
            self.assertEqual(after.get(payload), event_id,
                             f"record {payload} changed its event id")
        # A response id stays the authoritative identity.
        self.assertIn("antigravity:response:RESP-1", after.values())


if __name__ == "__main__":
    unittest.main()
