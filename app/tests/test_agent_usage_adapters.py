"""Pure adapter/IR regression tests (no Flask application required)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_usage.adapters import (
    cline, codex, craft_agent, dsh, grok, hermes, kiro, kimi_code, opencode,
    pi_common, workbuddy,
)
from app.services.agent_usage import cindy_ledger
from app.services.agent_usage.ir import UsageEvent
from app.services.agent_usage.registry import ADAPTERS


class AgentUsageAdapterTestCase(unittest.TestCase):
    def test_registry_matches_reference_agent_set(self) -> None:
        self.assertEqual(len(ADAPTERS), 27)
        self.assertEqual(set(ADAPTERS), {
            "claude-code", "codex", "grok", "copilot-cli", "craft-agent",
            "cursor", "dimagent", "gemini-cli", "opencode", "openclaw",
            "omp", "pi-coding-agent", "qwen-code", "kimi-code", "amp",
            "alma", "droid", "dsh", "antigravity", "trae-cli", "hermes",
            "kiro", "mimocode", "cline", "roo-code", "workbuddy", "zcode",
        })

    def test_craft_agent_and_hermes_use_documented_home_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            craft_root = root / "craft"
            session_dir = craft_root / "workspaces" / "project" / ".pi-sessions"
            session_dir.mkdir(parents=True)
            session = session_dir / "session.jsonl"
            session.write_text("{}\n", encoding="utf-8")

            hermes_root = root / "hermes"
            hermes_root.mkdir()
            hermes_db = hermes_root / "state.db"
            with sqlite3.connect(hermes_db) as connection:
                connection.execute("""CREATE TABLE sessions (
                    id TEXT, model TEXT, started_at TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, cache_read_tokens INTEGER,
                    reasoning_tokens INTEGER
                )""")
                connection.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                    ("session-1", "hermes-model", "2026-08-24T00:00:00Z", 1, 2, 0, 0),
                )

            with patch.dict(os.environ, {
                    "CRAFT_AGENT_DIR": str(craft_root),
                    "HERMES_HOME": str(hermes_root)}):
                self.assertEqual(craft_agent.discover({})[0].path, session)
                self.assertEqual(len(hermes.discover({})), 1)

    def test_pi_does_not_scan_omp_store_when_pi_home_points_there(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            omp_root = Path(directory) / ".omp"
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(omp_root)}):
                self.assertEqual(pi_common.pi_roots({}, "pi-coding-agent"), [])

    def test_omp_also_follows_pi_home_when_it_is_an_omp_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            omp_root = Path(directory) / ".omp"
            sessions = omp_root / "sessions"
            sessions.mkdir(parents=True)
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(omp_root)}):
                self.assertIn(sessions, pi_common.pi_roots({}, "omp"))

    def test_dsh_and_grok_session_overrides_are_direct_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "relocated-store"
            root.mkdir()
            with patch.dict(os.environ, {
                    "VIBE_USAGE_DSH_SESSIONS": str(root),
                    "VIBE_USAGE_GROK_SESSIONS": str(root)}):
                self.assertEqual(dsh._sessions_root({}), root)
                self.assertEqual(grok.discover({}), [])

    def test_ir_maps_exclusive_buckets_and_reasoning(self) -> None:
        event = UsageEvent.from_buckets(
            model="model", input_tokens=10, cached_input_tokens=5,
            output_tokens=2, reasoning_output_tokens=3,
            requested_at="2026-08-24T00:00:00Z", event_id="test:1",
        )
        self.assertEqual((event.prompt_tokens, event.completion_tokens,
                          event.cache_read_tokens, event.total_tokens),
                         (15, 5, 5, 20))

    def test_opencode_accepts_custom_sqlite_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE message(data TEXT, session_id TEXT)")
            connection.execute(
                "INSERT INTO message(data,session_id) VALUES(?,?)",
                (json.dumps({
                    "role": "assistant",
                    "time": {"created": 1782720000000},
                    "modelID": "opencode-model",
                    "tokens": {"input": 10, "output": 2, "reasoning": 1,
                                "cache": {"read": 5}},
                    "path": {"root": "/work/project"},
                }), "session-1"),
            )
            connection.commit()
            connection.close()

            item = opencode.discover({"config": {"data_root": str(path)}})[0]
            events = opencode.parse(item).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].model, events[0].prompt_tokens,
                              events[0].completion_tokens,
                              events[0].cache_read_tokens),
                             ("opencode-model", 10, 3, 5))

    def test_codex_coalesces_live_and_archived_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "sessions" / "2026" / "08" / "24"
            archived = root / "archived_sessions"
            live.mkdir(parents=True)
            archived.mkdir()
            session_id = "11111111-2222-3333-4444-555555555555"

            def token(at: str, value: int) -> str:
                return json.dumps({
                    "type": "event_msg", "timestamp": at,
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": value, "output_tokens": 2,
                            "cached_input_tokens": 1, "total_tokens": value + 2,
                        },
                    }},
                })

            header = json.dumps({
                "type": "session_meta", "payload": {"id": session_id},
            })
            name = f"rollout-20260824010000-{session_id}.jsonl"
            (live / name).write_text("\n".join([header, token(
                "2026-08-24T01:00:00Z", 10)]) + "\n", encoding="utf-8")
            (archived / name).write_text("\n".join([header, token(
                "2026-08-24T01:00:00Z", 10), token(
                "2026-08-24T01:01:00Z", 20)]) + "\n", encoding="utf-8")

            sources = codex.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].path, archived / name)
            events = codex.parse(sources[0]).events
            self.assertEqual(len(events), 2)
            self.assertTrue(all(event.event_id.startswith(
                f"codex:session:{session_id}:" ) for event in events))

    def test_dsh_plain_session_and_seed_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions" / "project-key"
            parent_dir = root / "parent"
            child_dir = root / "child"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir(parents=True)

            def user(seq: int, at: str) -> dict:
                return {"seq": seq, "type": "user/message", "time": at,
                        "data": {"source": {"kind": "user"}}}

            def assistant(seq: int, at: str, value: int) -> dict:
                return {
                    "seq": seq, "type": "assistant/message", "time": at,
                    "data": {"message": {"source": {"model": "dsh-model"}},
                             "usage": {"inputTokens": value,
                                       "cacheWriteTokens": 2,
                                       "cacheReadTokens": 3,
                                       "outputTokens": 10,
                                       "reasoningTokens": 4}},
                }

            parent_records = [
                {"type": "session", "version": 0, "id": "parent",
                 "cwd": "/work/parent"},
                user(0, "2026-08-24T01:00:00Z"),
                assistant(1, "2026-08-24T01:00:01Z", 10),
            ]
            child_records = [
                {"type": "session", "version": 0, "id": "child",
                 "parentSession": "parent", "seedLength": 2,
                 "cwd": "/work/child"},
                user(0, "2026-08-24T01:10:00Z"),
                assistant(1, "2026-08-24T01:10:01Z", 10),
                user(2, "2026-08-24T01:11:00Z"),
                assistant(3, "2026-08-24T01:11:01Z", 20),
            ]
            (parent_dir / "session.jsonl").write_text(
                "\n".join(json.dumps(value) for value in parent_records) + "\n",
                encoding="utf-8")
            (child_dir / "session.jsonl").write_text(
                "\n".join(json.dumps(value) for value in child_records) + "\n",
                encoding="utf-8")

            sources = dsh.discover({"config": {"data_root": str(root.parent.parent)}})
            self.assertEqual(len(sources), 2)
            skips = dsh.replay_skips(sources)
            child_source = next(item for item in sources if item.path.parent.name == "child")
            self.assertEqual(skips[str(child_source.path)], 2)
            events = dsh.parse(child_source, skip_token_count=skips[str(child_source.path)]).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].project, events[0].prompt_tokens,
                              events[0].completion_tokens,
                              events[0].cache_read_tokens),
                             ("child", 25, 10, 3))

    def test_kiro_native_stream_uses_sidecar_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kiro"
            sessions = root / "sessions" / "cli"
            sessions.mkdir(parents=True)
            (sessions / "session-1.json").write_text(
                json.dumps({"cwd": "/work/project"}), encoding="utf-8")
            records = [
                {"kind": "Prompt", "data": {
                    "content": [{"kind": "text", "data": "a" * 40}],
                    "meta": {"timestamp": 1782720000},
                }},
                {"kind": "AssistantMessage", "data": {"content": [
                    {"kind": "thinking", "data": {
                        "text": "t" * 20, "signature": "s" * 400,
                        "modelId": "kiro-model",
                    }},
                    {"kind": "text", "data": "o" * 24},
                ]}},
            ]
            (sessions / "session-1.jsonl").write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8")

            item = kiro.discover({"config": {"data_root": str(root)}})[0]
            events = kiro.parse(item).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].model, events[0].project,
                              events[0].prompt_tokens,
                              events[0].completion_tokens,
                              events[0].cache_read_tokens),
                             ("kiro-model", "project", 10, 11, 0))

    def test_kiro_q_client_snapshots_become_credit_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "User").mkdir(parents=True)
            log_dir = root / "logs" / "20260824T120000" / "window" / "ext" / "kiro.kiroAgent"
            log_dir.mkdir(parents=True)

            def line(at: str, current: float) -> str:
                return f"{at} [info] " + json.dumps({
                    "commandName": "GetUsageLimitsCommand",
                    "output": {"usageBreakdownList": [{
                        "resourceType": "CREDIT", "unit": "INVOCATIONS",
                        "currentUsage": current, "nextDateReset": "2026-09-01T00:00:00Z",
                    }]},
                })

            (log_dir / "q-client.log").write_text("\n".join([
                line("2026-08-24 12:00:00.000", 100),
                line("2026-08-24 12:10:00.000", 125.5),
                line("2026-08-24 12:40:00.000", 130),
            ]) + "\n", encoding="utf-8")
            item = kiro.discover({"config": {"data_root": str(root)}})[0]
            events = kiro.parse(item).events
            self.assertEqual([event.completion_tokens for event in events], [25, 5])

    def test_kimi_current_wire_format_uses_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kimi-code"
            wire_dir = root / "sessions" / "wd_project_hash" / "session-1" / "agents" / "main"
            wire_dir.mkdir(parents=True)
            session_dir = wire_dir.parent.parent
            (root / "session_index.jsonl").write_text(
                json.dumps({"sessionDir": str(session_dir), "workDir": "/work/project"}) + "\n",
                encoding="utf-8")
            (wire_dir / "wire.jsonl").write_text(json.dumps({
                "type": "usage.record", "model": "kimi-model", "time": 1782720000000,
                "usage": {"inputOther": 10, "inputCacheCreation": 2,
                           "inputCacheRead": 3, "output": 4},
            }) + "\n", encoding="utf-8")

            item = kimi_code.discover({"config": {"data_root": str(root)}})[0]
            events = kimi_code.parse(item).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].model, events[0].project,
                              events[0].prompt_tokens,
                              events[0].cache_read_tokens),
                             ("kimi-model", "project", 15, 3))

    def test_kimi_subagent_wire_events_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kimi-code"
            session_dir = root / "sessions" / "wd_project_hash" / "session-1"
            main = session_dir / "agents" / "main"
            child = session_dir / "agents" / "agent-0"
            main.mkdir(parents=True)
            child.mkdir(parents=True)
            (root / "session_index.jsonl").write_text(
                json.dumps({"sessionDir": str(session_dir), "workDir": "/work/project"}) + "\n",
                encoding="utf-8")

            def record(value: int) -> str:
                return json.dumps({
                    "type": "usage.record", "model": "kimi-model",
                    "time": 1782720000000,
                    "usage": {"inputOther": value, "output": 1},
                })

            (main / "wire.jsonl").write_text(record(10) + "\n", encoding="utf-8")
            (child / "wire.jsonl").write_text(record(20) + "\n", encoding="utf-8")
            items = kimi_code.discover({"config": {"data_root": str(root)}})
            events = [event for item in items for event in kimi_code.parse(item).events]
            self.assertEqual(len(events), 2)
            self.assertEqual(sum(event.prompt_tokens for event in events), 30)

    def test_cindy_ledger_is_a_codex_source_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "CindyGlobal"
            root.mkdir()
            path = root / "cindy-owner.db"
            with sqlite3.connect(path) as connection:
                connection.execute("""
                    CREATE TABLE daily_model_usage (
                        day TEXT, agent_kind TEXT, model TEXT,
                        input_tokens INTEGER, output_tokens INTEGER,
                        cache_read_tokens INTEGER, cache_create_tokens INTEGER
                    )
                """)
                connection.execute(
                    "INSERT INTO daily_model_usage VALUES(?,?,?,?,?,?,?)",
                    ("2026-08-22", "codex", "gpt-cindy", 20, 2, 10, 1),
                )
            items = cindy_ledger.discover(
                {"config": {"cindy_dirs": str(root)}}, "codex")
            self.assertEqual(len(items), 1)
            event = cindy_ledger.parse(items[0], "codex").events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (31, 2, 10, 23))

    def test_cline_prefers_the_larger_migrated_task_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            new = root / "new"

            def write_task(base: Path, task_id: str, project: str, records: list[dict]) -> None:
                (base / "state").mkdir(parents=True)
                task_dir = base / "tasks" / task_id
                task_dir.mkdir(parents=True)
                (base / "state" / "taskHistory.json").write_text(json.dumps([{
                    "id": task_id, "ulid": "shared", "cwd": f"/work/{project}",
                }]), encoding="utf-8")
                (task_dir / "ui_messages.json").write_text(json.dumps(records), encoding="utf-8")

            def api(value: int) -> dict:
                return {"type": "say", "say": "api_req_started", "ts": 1782720000000,
                        "text": json.dumps({"model": "cline-model", "tokensIn": value,
                                             "tokensOut": 2})}

            write_task(old, "old-task", "old-project", [api(999)])
            write_task(new, "new-task", "new-project", [api(10), api(20)])
            # data_root accepts one path; use the environment override for the
            # multi-root shape exercised by the reference parser.
            with patch.dict(os.environ, {
                    "VIBE_USAGE_CLINE_DIRS": f"{old}{os.pathsep}{new}"}):
                items = cline.discover({})
            self.assertEqual(len(items), 1)
            events = cline.parse(items[0]).events
            self.assertEqual(len(events), 2)
            self.assertEqual((events[0].project,
                              sum(event.prompt_tokens for event in events)),
                             ("new-project", 30))

    def test_workbuddy_ignores_non_completed_assistant_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            projects = Path(directory) / "projects" / "encoded"
            projects.mkdir(parents=True)
            records = [
                {"id": "partial", "type": "message", "status": "streaming",
                 "role": "assistant", "timestamp": 1782720000000,
                 "providerData": {"usage": {"inputTokens": 100, "outputTokens": 2}}},
                {"id": "done", "type": "message", "status": "completed",
                 "role": "assistant", "cwd": "/work/project",
                 "timestamp": 1782720001000,
                 "providerData": {"requestModelId": "wb-model",
                                  "usage": {"inputTokens": 100, "outputTokens": 20,
                                            "input_details": [{"cached_tokens": 40}]}}},
            ]
            path = projects / "session.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                            encoding="utf-8")
            item = workbuddy.discover({"config": {"data_root": str(Path(directory))}})[0]
            events = workbuddy.parse(item).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].model, events[0].project,
                              events[0].prompt_tokens,
                              events[0].cache_read_tokens),
                             ("wb-model", "project", 100, 40))


if __name__ == "__main__":
    unittest.main()
