"""02-F-09 transition: the one-time event_id re-key is complete and idempotent.

``record_identity`` replaced the positional record identities with content ones,
so a row imported before that change keeps an id no adapter will produce again
(its receipt no longer matches and the source history would be imported a
second time).  The importer re-keys those rows once, before the import pass,
per source kind:

* only ``event_id`` changes in ``agent_usage_receipts`` and ``request_log``;
  tokens, costs, ``pricing_status`` and timestamps stay byte-identical and no
  row is added or removed;
* completion is recorded per kind (``agent_usage_event_id_rekey_v2:<kind>`` in
  the machine-local ``sync_state`` table), so an unreadable source cannot keep
  the other kinds from finishing;
* ``dsh`` (whose identity also moved from a file position to the record's own
  message id) waits for a Zstandard decoder instead of scanning sessions it
  could never decode: no marker, no rewrite, and a warning — the other kinds
  still complete.

A legacy row whose record no longer exists in its source is left as it is: it
can never collide with a new id, and rewriting it would be a guess.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from app.services.agent_usage import importer
from app.services.agent_usage.adapters import zstd_reader
from app.services.agent_usage.registry import get_adapter
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class LegacyEventIdRekeyTest(AppDatabaseTestCase):
    RECORDS = [
        {"type": "message", "timestamp": "2026-09-21T14:13:20Z",
         "message": {"role": "assistant", "model": "claw-a",
                     "usage": {"input": 10, "output": 2}}},
        {"type": "message", "timestamp": "2026-09-21T14:14:20Z",
         "message": {"role": "assistant", "model": "claw-b",
                     "usage": {"input": 20, "output": 3}}},
    ]
    DSH_RECORDS = [
        {"type": "session", "version": 0, "id": "v0-session",
         "cwd": "/work/dsh"},
        {"seq": 0, "type": "user/message", "time": "2026-09-21T14:20:00Z",
         "data": {"source": {"kind": "user"}, "id": "user-1"}},
        {"type": "assistant/message", "time": "2026-09-21T14:20:01Z",
         "data": {"message": {"source": {"model": "dsh-model"}, "id": "m-1"},
                  "usage": {"inputTokens": 7, "outputTokens": 3}}},
    ]

    def _require_rekey(self):
        rekey = getattr(importer, "rekey_legacy_event_ids", None)
        if rekey is None:
            self.fail("02-F-09: importer.rekey_legacy_event_ids 不存在"
                      "（过渡期改键未实现）")
        return rekey

    def _openclaw_source(self) -> tuple[Path, list]:
        root = Path(self._temporary.name) / "openclaw"
        session = root / "agents" / "agent-1" / "run.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(json.dumps(record) for record in self.RECORDS) + "\n",
            encoding="utf-8")
        adapter = get_adapter("openclaw")
        sources = adapter.discover({"config": {"data_root": str(root)}})
        self.assertEqual(len(sources), 1)
        parsed = adapter.parse(sources[0])
        self.assertFalse(parsed.skipped, parsed.warnings)
        return root, list(parsed.events)

    def _dsh_source(self) -> tuple[Path, list]:
        root = Path(self._temporary.name) / "dsh"
        session = root / "sessions" / "proj" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            "\n".join(json.dumps(record) for record in self.DSH_RECORDS) + "\n",
            encoding="utf-8")
        adapter = get_adapter("dsh")
        sources = adapter.discover({"config": {"data_root": str(root)}})
        self.assertEqual(len(sources), 1)
        parsed = adapter.parse(sources[0])
        self.assertFalse(parsed.skipped, parsed.warnings)
        return root, list(parsed.events)

    def _software(self, name: str, kind: str, root: Path) -> int:
        return self.proxy_database().create_agent_software({
            "name": name, "agent_kind": kind,
            "config": {"data_root": str(root)},
        })

    def _seed_legacy_rows(self, software_id: int, events: list,
                          *, stale: bool = False) -> dict:
        """Insert positional ids for the real records (plus one orphan)."""
        expected = {}
        with sqlite_connection(self.proxy_path) as conn:
            for index, event in enumerate(events, start=1):
                legacy_id = f"legacy:{software_id}:{index}"
                conn.execute(
                    "INSERT INTO request_log("
                    "event_id,source_kind,account_id,agent_software_id,model,"
                    "prompt_tokens,completion_tokens,cache_read_tokens,"
                    "total_tokens,equivalent_cost,billed_usage_cost,"
                    "status_code,requested_at,pricing_status,duration_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (legacy_id, "import", software_id, software_id,
                     event.model, event.prompt_tokens, event.completion_tokens,
                     event.cache_read_tokens, event.total_tokens,
                     0.01 * index, 0.02 * index, 200, event.requested_at,
                     "frozen", 1234))
                conn.execute(
                    "INSERT INTO agent_usage_receipts(event_id,software_id) "
                    "VALUES(?,?)", (legacy_id, software_id))
                expected[legacy_id] = (
                    event.requested_at, event.model, event.prompt_tokens,
                    event.completion_tokens, event.cache_read_tokens,
                    event.total_tokens, 0.01 * index, 0.02 * index,
                    "frozen", 1234)
            if stale:
                conn.execute(
                    "INSERT INTO request_log("
                    "event_id,source_kind,account_id,agent_software_id,model,"
                    "prompt_tokens,completion_tokens,cache_read_tokens,"
                    "total_tokens,equivalent_cost,billed_usage_cost,"
                    "status_code,requested_at,pricing_status,duration_ms) "
                    "VALUES('legacy:orphan','import',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (software_id, software_id, "gone-model", 999, 0, 0, 999,
                     0.5, 0.5, 200, "2026-09-21T14:16:20Z", "frozen", 0))
                conn.execute(
                    "INSERT INTO agent_usage_receipts(event_id,software_id) "
                    "VALUES('legacy:orphan',?)", (software_id,))
        return expected

    def _rows(self) -> list[tuple]:
        with sqlite_connection(self.proxy_path) as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT event_id,requested_at,model,prompt_tokens,"
                "completion_tokens,cache_read_tokens,total_tokens,"
                "equivalent_cost,billed_usage_cost,pricing_status,duration_ms "
                "FROM request_log ORDER BY id")]

    def _receipt_ids(self) -> list[str]:
        with sqlite_connection(self.proxy_path) as conn:
            return [row[0] for row in conn.execute(
                "SELECT event_id FROM agent_usage_receipts ORDER BY event_id")]

    def _markers(self) -> set[str]:
        with sqlite_connection(self.proxy_path) as conn:
            return {row[0] for row in conn.execute(
                "SELECT key FROM sync_state WHERE key LIKE ?",
                (importer.EVENT_ID_REKEY_MARKER_PREFIX + ":%",))}

    def _marker(self, kind: str) -> str:
        return importer.event_id_rekey_marker(kind)

    def test_one_time_rekey_swaps_ids_only_and_second_run_is_a_noop(self) -> None:
        rekey = self._require_rekey()
        root, events = self._openclaw_source()
        software_id = self._software("openclaw-legacy", "openclaw", root)
        expected = self._seed_legacy_rows(software_id, events, stale=True)
        before_rows = self._rows()
        self.assertEqual(len(before_rows), len(events) + 1)

        changed = rekey(self.proxy_database())
        self.assertEqual(changed, len(events))

        after_rows = self._rows()
        self.assertEqual(len(after_rows), len(before_rows),
                         "the re-key must not add or remove a row")
        new_ids = [event.event_id for event in events]
        self.assertEqual(sorted(row[0] for row in after_rows),
                         sorted(new_ids + ["legacy:orphan"]))
        self.assertEqual(sorted(self._receipt_ids()),
                         sorted(new_ids + ["legacy:orphan"]))

        # Each re-keyed row keeps every non-id column, in the same order.
        expected_rows = list(expected.values())
        for row in after_rows:
            if row[0] == "legacy:orphan":
                continue
            self.assertIn(row[1:], expected_rows,
                          f"{row[0]} changed more than its id")
        # The orphan stays on its positional id: its record is gone.
        self.assertEqual(
            [row[0] for row in after_rows if row[0] == "legacy:orphan"],
            ["legacy:orphan"])

        # Completion is recorded per kind, not globally.
        self.assertIn(self._marker("openclaw"), self._markers())
        self.assertNotIn(self._marker("dsh"), self._markers())

        # Second run: strict no-op (same rows, same values, nothing written).
        snapshot = self._rows()
        self.assertEqual(rekey(self.proxy_database()), 0)
        self.assertEqual(self._rows(), snapshot)
        self.assertEqual(sorted(self._receipt_ids()),
                         sorted(new_ids + ["legacy:orphan"]))

    def test_dsh_waits_for_a_decoder_while_other_kinds_complete(self) -> None:
        """The dsh kind is skipped (no marker) when no zstd library exists."""
        rekey = self._require_rekey()
        openclaw_root, openclaw_events = self._openclaw_source()
        dsh_root, dsh_events = self._dsh_source()
        openclaw_id = self._software("openclaw-legacy", "openclaw",
                                     openclaw_root)
        dsh_id = self._software("dsh-legacy", "dsh", dsh_root)
        self._seed_legacy_rows(openclaw_id, openclaw_events)
        dsh_expected = self._seed_legacy_rows(dsh_id, dsh_events)
        dsh_legacy_ids = set(dsh_expected)

        available = mock.Mock(return_value=False)
        with mock.patch.object(zstd_reader, "available", available), \
                self.assertLogs("app.services.agent_usage.importer",
                                level="WARNING") as captured:
            changed = rekey(self.proxy_database())
        self.assertTrue(available.called,
                        "the dsh path must consult the decoder availability")
        messages = " ".join(record.getMessage() for record in captured.records)
        self.assertIn("waiting for dsh", messages)

        # The other kind completed and marked itself; dsh did not.
        self.assertEqual(changed, len(openclaw_events))
        self.assertIn(self._marker("openclaw"), self._markers())
        self.assertNotIn(self._marker("dsh"), self._markers())
        dsh_rows = {row[0] for row in self._rows()} & dsh_legacy_ids
        self.assertEqual(dsh_rows, dsh_legacy_ids,
                         "dsh rows must keep their old ids while waiting")

        # Once a decoder is available the dsh rewrite completes by itself.
        with mock.patch.object(zstd_reader, "available", return_value=True):
            changed = rekey(self.proxy_database())
        self.assertEqual(changed, len(dsh_events))
        self.assertIn(self._marker("dsh"), self._markers())
        current_ids = {event.event_id for event in dsh_events}
        rows_by_id = {row[0]: row for row in self._rows()}
        for event_id in current_ids:
            self.assertIn(event_id, rows_by_id, "dsh row was not re-keyed")
            self.assertIn(rows_by_id[event_id][1:],
                          list(dsh_expected.values()),
                          f"{event_id} changed more than its id")
        # And the second pass over the now-complete kinds is a no-op.
        self.assertEqual(rekey(self.proxy_database()), 0)


if __name__ == "__main__":
    unittest.main()
