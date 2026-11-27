"""OMP (Oh My Pi) adapter seam and recorded-sample golden tests.

TB05-F07: ``omp.parse`` / ``omp.discover`` were never called directly — only the
shared ``pi_common`` reader was covered through other adapters, so a broken
delegation, KIND label or event prefix would not be noticed.

TB05-F08: every agent fixture in this suite was hand-written from the parser
author's understanding of the format.  ``fixtures/omp/recorded-samples.json`` is
a sanitized capture kept in the reference implementation's container shape
(``provenance`` + ``files`` map), so one golden case now pins the parser against
real-world record shapes, including fields it does not read.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.agent_usage.adapters import omp

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "omp"
           / "recorded-samples.json")


def _write_session(root: Path, name: str, records: list[dict],
                   project: str = "proj-alpha") -> Path:
    path = root / project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                    encoding="utf-8")
    return path


class OmpSeamTest(unittest.TestCase):
    def test_omp_discover_and_parse_seam(self) -> None:
        self.assertEqual(omp.KIND, "omp")
        self.assertEqual(omp.LABEL, "Oh My Pi")
        self.assertEqual(omp.DEFAULT_PATH.name, "sessions")
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            _write_session(sessions, "session-1.jsonl", [
                {"type": "session", "version": 3, "id": "omp-session",
                 "timestamp": "2026-11-05T08:47:42.468Z",
                 "cwd": "/work/omp-project"},
                {"type": "message", "id": "omp-message-1",
                 "timestamp": "2026-11-05T08:47:43.120Z",
                 "message": {"role": "assistant", "model": "omp-model",
                             "usage": {"input": 7, "output": 3}}},
            ])
            sources = omp.discover({"config": {"data_root": str(sessions)}})
            self.assertEqual(len(sources), 1)
            parsed = omp.parse(sources[0])
            # Repeated runs on the same capture must produce identical ids:
            # unknown captured fields cannot leak into the record identity.
            rerun_ids = ({event.event_id for event in omp.parse(sources[0]).events})
        self.assertFalse(parsed.skipped, parsed.warnings)
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        self.assertEqual((event.model, event.prompt_tokens,
                          event.completion_tokens, event.total_tokens),
                         ("omp-model", 7, 3, 10))
        self.assertEqual(event.project, "omp-project")
        self.assertEqual(event.session_id, "omp-session")
        self.assertTrue(event.event_id.startswith("omp:"), event.event_id)


class OmpRecordedSampleTest(unittest.TestCase):
    def test_recorded_capture_reproduces_golden_usage(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertIn("OMP", document["provenance"])
        self.assertEqual(list(document["files"]), [
            "omp--2026-11-05T08-47-42-468Z_01a1aae4-7984-739b-8fda-527cc87e9f41.jsonl",
        ])

        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            for key, records in document["files"].items():
                _write_session(sessions, key.split("--", 1)[1], records)
            sources = omp.discover({"config": {"data_root": str(sessions)}})
            self.assertEqual(len(sources), 1)
            parsed = omp.parse(sources[0])
            # Repeated runs on the same capture must produce identical ids:
            # unknown captured fields cannot leak into the record identity.
            rerun_ids = {event.event_id for event in omp.parse(sources[0]).events}

        self.assertFalse(parsed.skipped, parsed.warnings)
        # The capture contains four messages: a user turn, two assistant turns
        # with usage and one assistant turn without usage.  A turn without a
        # usage block must not become a zero-token event.
        self.assertEqual(len(parsed.events), 2)
        first, second = parsed.events
        self.assertEqual(
            (first.model, first.prompt_tokens, first.completion_tokens,
             first.cache_read_tokens, first.total_tokens),
            ("deepseek/deepseek-v4.1-flash", 156, 40, 30, 196))
        self.assertEqual(
            (second.model, second.prompt_tokens, second.completion_tokens,
             second.cache_read_tokens, second.total_tokens),
            ("deepseek-v4.1", 12, 5, 2, 17))
        for event in parsed.events:
            self.assertTrue(event.event_id.startswith("omp:"), event.event_id)
            self.assertEqual(event.project, "proj-alpha")
            self.assertEqual(event.session_id,
                             "01a1aae4-7984-739b-8fda-527cc87e9f41")
        # Unknown captured fields (recorderExtension/telemetry/
        # providerMetadata/unknownUsageField) are ignored: the parsed identity
        # is stable across repeated runs on the same capture.
        self.assertEqual(rerun_ids, {event.event_id for event in parsed.events})


if __name__ == "__main__":
    unittest.main()
