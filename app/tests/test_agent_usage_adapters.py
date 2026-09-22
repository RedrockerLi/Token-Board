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
    alma, amp, antigravity, claude_code, cline, codearts_agent, codebuddy,
    codex, cola, copilot_cli, craft_agent, cursor, devin, dimagent, droid,
    dsh, gemini_cli, grok, hermes, kiro, kimi_code, mcode, mimocode,
    openclaw, opencode, pi_common, qoder, qoder_cn, roo_code, qwen_code,
    trae_cli, workbuddy, zcode,
)
from app.services.agent_usage import cindy_ledger
from app.services.agent_usage.ir import UsageEvent, UsageSource
from app.services.agent_usage.registry import ADAPTERS
from app.tests.support import sqlite_connection


class AgentUsageAdapterTestCase(unittest.TestCase):
    def assert_one_event(self, parsed, *, model: str, prompt: int,
                         completion: int, cache: int, total: int,
                         project: str | None = None,
                         session_id: str | None = None) -> None:
        self.assertFalse(parsed.skipped)
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        self.assertEqual(
            (event.model, event.prompt_tokens, event.completion_tokens,
             event.cache_read_tokens, event.total_tokens),
            (model, prompt, completion, cache, total),
        )
        if project is not None:
            self.assertEqual(event.project, project)
        if session_id is not None:
            self.assertEqual(event.session_id, session_id)
        self.assertTrue(event.event_id)

    def test_registry_matches_reference_agent_set(self) -> None:
        self.assertEqual(len(ADAPTERS), 34)
        self.assertEqual(set(ADAPTERS), {
            "claude-code", "codex", "grok", "copilot-cli", "craft-agent",
            "cursor", "dimagent", "gemini-cli", "opencode", "openclaw",
            "omp", "pi-coding-agent", "qwen-code", "kimi-code", "amp",
            "alma", "droid", "dsh", "antigravity", "trae-cli", "hermes",
            "kiro", "mimocode", "cline", "roo-code", "workbuddy", "zcode",
            "mcode", "cola", "qoder", "qoder-cn", "devin", "codebuddy",
            "codearts-agent",
        })

    def test_copilot_cli_parses_shutdown_model_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-state" / "session-1"
            root.mkdir(parents=True)
            (root / "events.jsonl").write_text("\n".join([
                json.dumps({
                    "type": "session.start", "timestamp": "2026-09-20T15:00:00Z",
                    "data": {"context": {"gitRoot": "/work/copilot-demo"}},
                }),
                json.dumps({
                    "type": "session.shutdown", "timestamp": "2026-09-20T15:01:00Z",
                    "data": {"modelMetrics": {
                        "gpt-5-codex": {"usage": {
                            "inputTokens": 100, "cacheReadTokens": 20,
                            "outputTokens": 7,
                        }},
                    }},
                }),
            ]) + "\n", encoding="utf-8")

            items = copilot_cli.discover({"config": {"data_root": str(root.parent)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                copilot_cli.parse(items[0]), model="gpt-5-codex",
                prompt=100, completion=7, cache=20, total=107,
                project="copilot-demo", session_id="session-1")

    def test_dimagent_deduplicates_mirrored_usage_ledger_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "dimcode.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE sessions (sessionId TEXT, cwd TEXT)")
            conn.execute("""CREATE TABLE usage_ledger (
                ledgerId TEXT, runId TEXT, providerId TEXT, modelId TEXT,
                usage TEXT, cost TEXT, createdAt TEXT, sessionId TEXT
            )""")
            usage = json.dumps({
                "promptTokens": 120, "cacheReadTokens": 20,
                "completionTokens": 8,
            })
            row = ("run-1", "provider", "dim-model", usage, "0.1",
                   "2026-09-20T15:00:00Z", "session-1")
            conn.execute("INSERT INTO sessions VALUES (?,?)",
                         ("session-1", "/work/dim-demo"))
            conn.execute("INSERT INTO usage_ledger VALUES (?,?,?,?,?,?,?,?)",
                         ("entry-1", *row))
            conn.execute("INSERT INTO usage_ledger VALUES (?,?,?,?,?,?,?,?)",
                         ("ledger_mirror", *row))
            conn.commit()
            conn.close()

            items = dimagent.discover({"config": {"data_root": str(db)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                dimagent.parse(items[0]), model="dim-model",
                prompt=120, completion=8, cache=20, total=128,
                project="dim-demo")

    def test_gemini_cli_parses_native_usage_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.json"
            path.write_text(json.dumps({
                "directories": ["/work/gemini-demo"],
                "messages": [
                    {"role": "user", "timestamp": "2026-09-20T15:00:00Z"},
                    {"type": "gemini", "model": "gemini-2.5-pro",
                     "createTime": "2026-09-20T15:01:00Z",
                     "usageMetadata": {
                         "promptTokenCount": 100,
                         "cachedContentTokenCount": 20,
                         "candidatesTokenCount": 12,
                         "thoughtsTokenCount": 2,
                     }},
                ],
            }), encoding="utf-8")

            items = gemini_cli.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                gemini_cli.parse(items[0]), model="gemini-2.5-pro",
                prompt=100, completion=12, cache=20, total=112,
                project="gemini-demo", session_id="session")

    def test_openclaw_discovers_profile_session_and_reads_usage_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "openclaw-demo"
            path = root / "agents" / "session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "type": "message", "timestamp": "2026-09-20T15:00:00Z",
                "message": {
                    "role": "assistant", "model": "openclaw-model",
                    "usage": {
                        "inputTokens": 100, "cacheCreationInputTokens": 5,
                        "cacheRead": 20, "outputTokens": 8,
                    },
                },
            }) + "\n", encoding="utf-8")

            items = openclaw.discover({"config": {"data_root": str(root / "agents")}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                openclaw.parse(items[0]), model="openclaw-model",
                prompt=125, completion=8, cache=20, total=133,
                project="openclaw-demo", session_id="session")

    def test_qwen_code_deduplicates_repeated_message_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "qwen-tmp" / "2026" / "chats"
            root.mkdir(parents=True)
            event = {
                "type": "assistant", "uuid": "message-1",
                "timestamp": "2026-09-20T15:00:00Z", "cwd": "/work/qwen-demo",
                "model": "qwen3-coder",
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "cachedContentTokenCount": 20,
                    "candidatesTokenCount": 12,
                    "thoughtsTokenCount": 2,
                },
            }
            (root / "session.jsonl").write_text(
                "\n".join(json.dumps(value) for value in (event, event)) + "\n",
                encoding="utf-8")

            items = qwen_code.discover({"config": {"data_root": str(root.parent.parent)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                qwen_code.parse(items[0]), model="qwen3-coder",
                prompt=100, completion=12, cache=20, total=112,
                project="qwen-demo", session_id="session")

    def test_amp_prefers_usage_ledger_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "threads"
            root.mkdir()
            (root / "T-1.json").write_text(json.dumps({
                "id": "amp-thread-1",
                "messages": [{"usage": {"cacheCreationInputTokens": 5,
                                           "cacheReadInputTokens": 20}}],
                "usageLedger": {"events": [{
                    "toMessageId": 0, "model": "amp-model",
                    "timestamp": "2026-09-20T15:00:00Z",
                    "tokens": {"input": 100, "output": 8},
                }]},
            }), encoding="utf-8")

            items = amp.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                amp.parse(items[0]), model="amp-model",
                prompt=125, completion=8, cache=20, total=133,
                session_id="amp-thread-1")

    def test_alma_reads_usage_records_and_workspace_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "chat_threads.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT) WITHOUT ROWID")
            conn.execute(
                "CREATE TABLE chat_threads (id TEXT PRIMARY KEY, workspace_id INTEGER) WITHOUT ROWID")
            conn.execute("""CREATE TABLE usage_records (
                timestamp TEXT, model TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cached_input_tokens INTEGER,
                reasoning_tokens INTEGER, cache_write_input_tokens INTEGER,
                thread_id TEXT
            )""")
            conn.execute("INSERT INTO workspaces VALUES (1, 'alma-demo')")
            conn.execute("INSERT INTO chat_threads VALUES ('thread-1', 1)")
            conn.execute(
                "INSERT INTO usage_records VALUES (?,?,?,?,?,?,?,?)",
                ("2026-09-20T15:00:00Z", "provider:alma-model", 100, 8,
                 20, 2, 5, "thread-1"))
            conn.commit()
            conn.close()

            items = alma.discover({"config": {"data_root": str(db)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                alma.parse(items[0]), model="alma-model",
                prompt=125, completion=10, cache=20, total=135,
                project="alma-demo")

    def test_droid_combines_session_and_settings_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project-demo"
            root.mkdir()
            session = root / "session.jsonl"
            session.write_text(json.dumps({
                "type": "message", "timestamp": "2026-09-20T15:00:00Z",
            }) + "\n", encoding="utf-8")
            (root / "session.settings.json").write_text(json.dumps({
                "model": "auto",
                "tokenUsage": {
                    "inputTokens": 100, "cacheReadTokens": 20,
                    "cacheCreationTokens": 5, "outputTokens": 10,
                    "thinkingTokens": 2,
                },
            }), encoding="utf-8")

            items = droid.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                droid.parse(items[0]), model="droid-auto",
                prompt=125, completion=10, cache=20, total=135,
                project="demo", session_id="session")

    def test_trae_cli_selects_primary_trace_spans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "session-1"
            root.mkdir()
            (root / "session.json").write_text(json.dumps({
                "metadata": {"cwd": "/work/trae-demo", "model_name": "fallback"},
            }), encoding="utf-8")
            (root / "traces.jsonl").write_text(json.dumps({
                "startTime": 1789916400000,
                "tags": [
                    {"key": "span.category", "value": "model.stream.eino"},
                    {"key": "model.name", "value": "trae-model"},
                    {"key": "usage.input_tokens", "value": 100},
                    {"key": "usage.output_tokens", "value": 10},
                    {"key": "usage.cache_read_tokens", "value": 20},
                    {"key": "usage.reasoning_tokens", "value": 2},
                ],
            }) + "\n", encoding="utf-8")

            items = trae_cli.discover({"config": {"data_root": str(root.parent)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                trae_cli.parse(items[0]), model="trae-model",
                prompt=120, completion=12, cache=20, total=132,
                project="trae-demo", session_id="session-1")

    def test_mimocode_reads_assistant_message_and_cache_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "mimocode.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
            conn.execute("CREATE TABLE message (session_id TEXT, time_created TEXT, data TEXT)")
            conn.execute("INSERT INTO session VALUES ('s1', '/work/mimo-demo')")
            conn.execute("INSERT INTO message VALUES ('s1', ?, ?)", (
                "2026-09-20T15:00:00Z", json.dumps({
                    "role": "assistant", "modelID": "mimo-model",
                    "time": {"created": "2026-09-20T15:00:00Z"},
                    "tokens": {"input": 100, "output": 10,
                               "reasoning": 2,
                               "cache": {"read": 20, "write": 5}},
                })))
            conn.commit()
            conn.close()

            items = mimocode.discover({"config": {"data_root": str(db)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                mimocode.parse(items[0]), model="mimo-model",
                prompt=125, completion=12, cache=20, total=137,
                project="mimo-demo", session_id="s1")

    def test_roo_code_reads_indexed_task_api_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "tasks" / "task-1"
            tasks.mkdir(parents=True)
            (root / "tasks" / "_index.json").write_text(json.dumps({
                "entries": [{"id": "task-1", "workspace": "/work/roo-demo",
                             "apiConfigName": "roo-default"}],
            }), encoding="utf-8")
            (tasks / "ui_messages.json").write_text(json.dumps([
                {"type": "say", "say": "api_req_started",
                 "ts": 1789916400000,
                 "text": json.dumps({
                     "model": "roo-model", "tokensIn": 100,
                     "cacheWrites": 5, "cacheReads": 20, "tokensOut": 8,
                 })},
            ]), encoding="utf-8")

            items = roo_code.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                roo_code.parse(items[0]), model="roo-model",
                prompt=125, completion=8, cache=20, total=133,
                project="roo-demo", session_id="task-1")

    def test_zcode_reads_sqlite_message_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
            conn.execute("CREATE TABLE message (session_id TEXT, time_created TEXT, data TEXT)")
            conn.execute("INSERT INTO session VALUES ('z1', '/work/zcode-demo')")
            conn.execute("INSERT INTO message VALUES ('z1', ?, ?)", (
                "2026-09-20T15:00:00Z", json.dumps({
                    "role": "assistant", "modelId": "zcode-model",
                    "tokens": {"input": 100, "output": 10,
                               "reasoning": 2,
                               "cache": {"read": 20, "write": 5}},
                    "path": {"root": "/work/zcode-demo"},
                })))
            conn.commit()
            conn.close()

            items = zcode.discover({"config": {"data_root": str(db)}})
            self.assertEqual(len(items), 1)
            self.assert_one_event(
                zcode.parse(items[0]), model="zcode-model",
                prompt=105, completion=10, cache=20, total=115,
                project="zcode-demo", session_id="z1")

    def test_claude_reads_cache_hits_and_applies_reference_fast_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "-work-project" / "session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "type": "assistant",
                "uuid": "call-1",
                "timestamp": "2026-09-20T15:00:00Z",
                "cwd": "/work/project",
                "message": {
                    "id": "message-1",
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 11,
                        "cache_read_input_tokens": 13,
                        "cache_creation_input_tokens": 17,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 5,
                            "ephemeral_1h_input_tokens": 12,
                        },
                        "output_tokens": 7,
                        "speed": "fast",
                    },
                },
            }) + "\n", encoding="utf-8")

            with patch.dict(os.environ, {"VIBE_USAGE_CLAUDE_DIRS": str(root)}):
                parsed = claude_code.parse(claude_code.discover({})[0])

            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            self.assertEqual(event.model, "claude-opus-4-8-fast")
            # request_log has no cache-creation column: cache writes stay in
            # the non-overlapping input projection, while cache reads remain
            # independently available to the dashboard as cache hits.
            self.assertEqual((event.prompt_tokens, event.cache_read_tokens,
                              event.completion_tokens, event.total_tokens),
                             (41, 13, 7, 48))

    def test_claude_extra_roots_are_additive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            extra = base / "extra"
            for root, name in ((primary, "one"), (extra, "two")):
                path = root / "projects" / "-work-project" / f"{name}.jsonl"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({
                    "type": "assistant",
                    "uuid": name,
                    "timestamp": "2026-09-20T15:00:00Z",
                    "cwd": "/work/project",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                    },
                }) + "\n", encoding="utf-8")

            with patch.dict(os.environ, {"VIBE_USAGE_CLAUDE_DIRS": str(primary)}):
                items = claude_code.discover({
                    "config": {"extra_roots": {"claude-code": [str(extra)]}},
                })
                parsed = [claude_code.parse(item) for item in items]

            self.assertEqual(len(items), 2)
            self.assertEqual(sum(len(batch.events) for batch in parsed), 2)

    def test_cline_sdk_session_artifact_is_imported_without_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "cline-session"
            session_dir = root / "data" / "sessions" / session_id
            session_dir.mkdir(parents=True)
            (session_dir / f"{session_id}.json").write_text(json.dumps({
                "version": 1, "session_id": session_id,
                "started_at": "2026-09-20T15:00:00Z",
                "workspace_root": "/work/project",
            }), encoding="utf-8")
            (session_dir / f"{session_id}.messages.json").write_text(json.dumps({
                "version": 1, "sessionId": session_id, "agent": "lead",
                "messages": [
                    {"id": "user-1", "role": "user", "ts": 1789916400000,
                     "content": "PRIVATE PROMPT"},
                    {"id": "assistant-1", "role": "assistant",
                     "ts": 1789916401000, "content": "PRIVATE RESPONSE",
                     "modelInfo": {"id": "cline-model"},
                     "metrics": {"inputTokens": 100,
                                 "cacheReadTokens": 30,
                                 "outputTokens": 20}},
                ],
            }), encoding="utf-8")

            with patch.dict(os.environ, {"VIBE_USAGE_CLINE_DIRS": str(root)}):
                items = cline.discover({})
                parsed = cline.parse(items[0])

            self.assertEqual(len(items), 1)
            self.assertEqual(len(parsed.events), 1)
            self.assertEqual((parsed.events[0].model,
                              parsed.events[0].prompt_tokens,
                              parsed.events[0].cache_read_tokens),
                             ("cline-model", 100, 30))

    def test_cline_sdk_io_failure_drops_one_artifact_but_keeps_valid_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "data" / "sessions"
            valid = sessions / "valid"
            broken = sessions / "broken"
            valid.mkdir(parents=True)
            broken.mkdir(parents=True)
            for session_dir, session_id in ((valid, "valid"), (broken, "broken")):
                (session_dir / f"{session_id}.json").write_text(json.dumps({
                    "version": 1, "session_id": session_id,
                    "started_at": "2026-09-20T15:00:00Z",
                    "workspace_root": "/work/project",
                }), encoding="utf-8")
            (valid / "valid.messages.json").write_text(json.dumps({
                "version": 1, "sessionId": "valid", "agent": "lead",
                "messages": [{
                    "id": "assistant-1", "role": "assistant",
                    "ts": 1789916401000, "modelInfo": {"id": "cline-model"},
                    "metrics": {"inputTokens": 10, "outputTokens": 2},
                }],
            }), encoding="utf-8")
            (broken / "broken.messages.json").write_text("{", encoding="utf-8")

            with patch.dict(os.environ, {"VIBE_USAGE_CLINE_DIRS": str(root)}):
                item = cline.discover({})[0]
                parsed = cline.parse(item)

            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            self.assertTrue(parsed.warnings)

    def test_grok_usage_ledger_without_updates_is_imported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "sessions" / "work-project" / "grok-session"
            session.mkdir(parents=True)
            (session / "summary.json").write_text(json.dumps({
                "info": {"cwd": "/work/project"},
                "current_model_id": "grok-model",
                "updated_at": "2026-09-20T15:00:00Z",
            }), encoding="utf-8")
            (session / "usage.json").write_text(json.dumps({
                "turns": [{
                    "inputTokens": 10, "cacheCreationTokens": 2,
                    "cachedReadTokens": 3, "outputTokens": 4,
                    "reasoningTokens": 1,
                }],
            }), encoding="utf-8")

            items = grok.discover({"config": {"data_root": str(Path(directory))}})
            self.assertEqual(len(items), 1)
            event = grok.parse(items[0]).events[0]
            self.assertEqual((event.model, event.project, event.prompt_tokens,
                              event.completion_tokens, event.cache_read_tokens),
                             ("grok-model", "project", 12, 4, 3))

    def test_antigravity_legacy_pb_uses_language_server_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = Path(directory) / "conversations"
            conversations.mkdir()
            legacy = conversations / "cascade-legacy.pb"
            legacy.write_bytes(b"opaque legacy history")
            trajectory = {
                "metadata": {"workspaces": [{
                    "workspaceFolderAbsoluteUri": "file:///work/project-legacy",
                }]},
                "generatorMetadata": [{"chatModel": {
                    "responseModel": "gemini-3-pro-high",
                    "chatStartMetadata": {"createdAt": "2026-09-07T06:30:02Z"},
                    "retryInfos": [{"usage": {
                        "responseId": "response-legacy",
                        "inputTokens": 100,
                        "outputTokens": 5,
                        "cacheReadTokens": 2,
                        "thinkingOutputTokens": 1,
                    }}],
                }}],
            }
            with patch.dict(os.environ, {
                    "VIBE_USAGE_ANTIGRAVITY_DIRS": str(conversations)}), \
                    patch.object(antigravity, "_legacy_trajectory",
                                 return_value=trajectory):
                items = antigravity.discover({})
                self.assertEqual(len(items), 1)
                parsed = antigravity.parse(items[0])

            event = parsed.events[0]
            self.assertEqual((event.model, event.project,
                              event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             ("gemini-3-pro", "project-legacy", 102, 6, 2, 108))

    def test_antigravity_sqlite_total_includes_cached_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "conversation.db"
            with sqlite_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE gen_metadata (idx INTEGER, data BLOB)"
                )
                connection.execute(
                    "CREATE TABLE steps (idx INTEGER, metadata BLOB)"
                )
                connection.execute(
                    "CREATE TABLE trajectory_metadata_blob (data BLOB)"
                )
                connection.execute(
                    "INSERT INTO gen_metadata VALUES(?,?)", (0, b"fixture")
                )

            record = {
                "input": 100,
                "output": 5,
                "cache": 2,
                "reasoning": 1,
                "response_id": "response-current",
                "display_name": "gemini-model",
                "response_model": "",
                "timestamp": "2026-09-07T06:30:02Z",
            }
            with patch.object(antigravity, "_metadata", return_value=record), \
                    patch.object(antigravity, "_workspace", return_value=None):
                parsed = antigravity.parse(UsageSource(path=path))

            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (102, 6, 2, 108))

    def test_mcode_reads_allowlisted_runtime_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-state.sqlite"
            with sqlite_connection(path) as connection:
                connection.execute("""CREATE TABLE local_runtime_sessions (
                    session_id TEXT PRIMARY KEY, workspace_dir TEXT,
                    project_workspace_dir TEXT
                )""")
                connection.execute("""CREATE TABLE local_runtime_token_usage (
                    session_id TEXT, model TEXT, ts INTEGER,
                    input_tokens INTEGER, output_tokens INTEGER,
                    reasoning_tokens INTEGER, cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER, raw TEXT
                )""")
                connection.execute(
                    "INSERT INTO local_runtime_sessions VALUES(?,?,?)",
                    ("session-1", "/tmp/workspace", "/work/project-a"),
                )
                connection.execute(
                    "INSERT INTO local_runtime_token_usage VALUES(?,?,?,?,?,?,?,?,?)",
                    ("session-1", "mcode-model", 1782720000000, 10, 13, 3, 5, 7,
                     '{"message":"must not be selected"}'),
                )

            item = mcode.discover({"config": {"data_root": str(path)}})[0]
            parsed = mcode.parse(item)
            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            self.assertEqual((event.model, event.project, event.prompt_tokens,
                              event.completion_tokens, event.cache_read_tokens,
                              event.total_tokens),
                             ("mcode-model", "project-a", 22, 16, 5, 38))

    def test_hermes_total_includes_cached_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with sqlite_connection(path) as connection:
                connection.execute("""CREATE TABLE sessions (
                    id TEXT, model TEXT, started_at TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, cache_read_tokens INTEGER,
                    reasoning_tokens INTEGER, cache_write_tokens INTEGER
                )""")
                connection.execute(
                    "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                    ("session-1", "hermes-model", "2026-08-24T00:00:00Z",
                     60, 20, 40, 5, 0),
                )

            event = hermes.parse(UsageSource(path=path)).events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (100, 20, 40, 120))

    def test_grok_total_includes_cached_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            (session / "summary.json").write_text(
                json.dumps({"current_model_id": "grok-model"}),
                encoding="utf-8",
            )
            path = session / "updates.jsonl"
            path.write_text(json.dumps({
                "timestamp": "2026-08-24T00:00:00Z",
                "params": {"update": {
                    "sessionUpdate": "turn_completed",
                    "usage": {"inputTokens": 100, "cachedReadTokens": 40,
                               "outputTokens": 20, "reasoningTokens": 5},
                }},
            }) + "\n", encoding="utf-8")

            event = grok.parse(UsageSource(
                path=path,
                context={"session_path": session, "session_id": "session-1"},
            )).events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (100, 20, 40, 120))

    def test_cursor_total_includes_cached_tokens(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return (
                    "Model,Date,Input (w/ Cache Write),Input (w/o Cache Write),"
                    "Output Tokens,Cache Read\n"
                    "cursor-model,2026-08-24T00:00:00Z,10,10,20,40\n"
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            with sqlite_connection(path) as connection:
                connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
                connection.execute(
                    "INSERT INTO ItemTable VALUES(?, ?)",
                    ("cursorAuth/accessToken", "token"),
                )

            with patch.object(cursor, "urlopen", return_value=Response()):
                event = cursor.parse(UsageSource(path=path)).events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (60, 20, 40, 80))

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
            with sqlite_connection(hermes_db) as connection:
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

    def test_codex_extra_root_finds_bounded_multica_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            container = Path(directory) / "multica"
            session_dir = container / "workspace" / "task" / "codex-home" / "sessions"
            session_dir.mkdir(parents=True)
            rollout = session_dir / "rollout-20260824010000-session.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")

            sources = codex.discover({"config": {
                "data_root": str(Path(directory) / "missing-primary"),
                "extra_roots": {"codex": [str(container)]},
            }})
            self.assertEqual([item.path for item in sources], [rollout])

    def test_pi_honors_session_directory_environment_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory) / "agent"
            default_sessions = agent / "sessions"
            env_sessions = Path(directory) / "env-sessions"
            settings_sessions = Path(directory) / "settings-sessions"
            default_sessions.mkdir(parents=True)
            env_sessions.mkdir()
            settings_sessions.mkdir()
            (agent / "settings.json").write_text(
                json.dumps({"sessionDir": str(settings_sessions)}),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {
                    "PI_CODING_AGENT_DIR": str(agent),
                    "PI_CODING_AGENT_SESSION_DIR": str(env_sessions)}):
                roots = pi_common.pi_roots({}, "pi-coding-agent")
            self.assertEqual(roots, [default_sessions, env_sessions, settings_sessions])

    def test_ir_maps_exclusive_buckets_and_reasoning(self) -> None:
        event = UsageEvent.from_buckets(
            model="model", input_tokens=10, cached_input_tokens=5,
            output_tokens=2, reasoning_output_tokens=3,
            requested_at="2026-08-24T00:00:00Z", event_id="test:1",
        )
        self.assertEqual((event.prompt_tokens, event.completion_tokens,
                          event.cache_read_tokens, event.total_tokens),
                         (15, 5, 5, 20))

    def test_pi_family_total_includes_cached_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "type": "session", "version": 1, "id": "pi-session",
                    "timestamp": "2026-08-24T00:00:00Z", "cwd": "/work/project",
                }),
                json.dumps({
                    "type": "message", "id": "message-1",
                    "timestamp": "2026-08-24T00:00:01Z",
                    "message": {
                        "role": "assistant", "model": "pi-model",
                        "usage": {"input": 60, "cacheRead": 40,
                                   "output": 20, "reasoning": 5},
                    },
                }),
            ]) + "\n", encoding="utf-8")

            event = pi_common.parse_pi(UsageSource(
                path=path, context={"sessions_root": root},
            ), "pi-coding-agent").events[0]
            self.assertEqual((event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             (100, 20, 40, 120))

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

    def test_opencode_merges_duplicate_messages_across_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.db"
            extra = root / "extra.db"

            def write_db(path: Path, input_tokens: int) -> None:
                connection = sqlite3.connect(path)
                connection.execute(
                    "CREATE TABLE message(id TEXT, data TEXT, session_id TEXT)"
                )
                connection.execute(
                    "INSERT INTO message VALUES(?,?,?)",
                    ("message-1", json.dumps({
                        "role": "assistant", "time": {"created": 1782720000000},
                        "modelID": "opencode-model",
                        "tokens": {"input": input_tokens, "output": 2,
                                    "cache": {"read": 1}},
                        "path": {"root": "/work/project"},
                    }), "session-1"),
                )
                connection.commit()
                connection.close()

            write_db(primary, 10)
            write_db(extra, 20)
            items = opencode.discover({"config": {
                "data_root": str(primary),
                "extra_roots": {"opencode": [str(extra)]},
            }})
            self.assertEqual(len(items), 1)
            events = opencode.parse(items[0]).events
            self.assertEqual(len(events), 1)
            self.assertEqual((events[0].prompt_tokens,
                              events[0].completion_tokens,
                              events[0].cache_read_tokens), (20, 2, 1))

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

    def test_codex_merges_same_session_continuation_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions" / "2026" / "09" / "01"
            root.mkdir(parents=True)
            session_id = "continuation-session"

            def token(at: str, value: int) -> dict:
                return {
                    "type": "event_msg", "timestamp": at,
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": value, "output_tokens": 2,
                            "total_tokens": value + 2,
                        },
                    }},
                }

            header = {"type": "session_meta", "payload": {"id": session_id}}
            context = {"type": "turn_context", "payload": {"model": "gpt-segment"}}
            first_dir = root / "live"
            second_dir = root / "archived"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / f"rollout-20260901010000-{session_id}.jsonl"
            second = second_dir / f"rollout-20260901010000-{session_id}.jsonl"
            first.write_text("\n".join(json.dumps(value) for value in [
                header, context, token("2026-09-01T01:00:00Z", 10),
                token("2026-09-01T01:01:00Z", 20),
            ]) + "\n", encoding="utf-8")
            second.write_text("\n".join(json.dumps(value) for value in [
                header, context, token("2026-09-01T01:02:00Z", 30),
            ]) + "\n", encoding="utf-8")

            sources = codex.discover({"config": {"data_root": str(Path(directory))}})
            self.assertEqual(len(sources), 1)
            self.assertEqual(len(sources[0].context["segments"]), 2)
            events = codex.parse(sources[0]).events
            self.assertEqual([event.prompt_tokens for event in events], [10, 20, 30])
            self.assertEqual(sum(event.prompt_tokens for event in events), 60)

    def test_codex_applies_service_tier_to_post_cutover_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions" / "2026" / "09" / "01"
            root.mkdir(parents=True)
            path = root / "rollout-tier.jsonl"
            path.write_text("\n".join([
                json.dumps({"type": "session_meta", "payload": {"id": "tier-session"}}),
                json.dumps({"type": "turn_context", "payload": {
                    "model": "gpt-tier", "service_tier": "fast",
                }}),
                json.dumps({"type": "event_msg", "timestamp": "2026-09-01T01:00:00Z",
                            "payload": {"type": "token_count", "info": {
                                "last_token_usage": {
                                    "input_tokens": 10, "output_tokens": 2,
                                    "total_tokens": 12,
                                },
                            }}}),
            ]) + "\n", encoding="utf-8")

            item = codex.discover({"config": {"data_root": str(Path(directory))}})[0]
            events = codex.parse(item).events
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].model, "gpt-tier-fast")

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

    def test_dsh_selects_highest_generation_and_matches_mixed_version_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions" / "project-key"
            parent = sessions / "parent"
            child = sessions / "child"
            parent.mkdir(parents=True)
            child.mkdir(parents=True)

            def user(seq: int, message_id: str, at: str) -> dict:
                return {
                    "seq": seq, "type": "user/message", "time": at,
                    "data": {"id": message_id, "source": {"kind": "user"}},
                }

            def assistant(seq: int, message_id: str, value: int, at: str) -> dict:
                return {
                    "seq": seq, "type": "assistant/message", "time": at,
                    "data": {
                        "message": {"id": message_id,
                                    "source": {"model": "dsh-model"}},
                        "usage": {"inputTokens": value, "outputTokens": 10,
                                   "reasoningTokens": 2},
                    },
                }

            inherited = [
                user(10, "u1", "2026-09-01T01:00:00Z"),
                assistant(11, "a1", 100, "2026-09-01T01:00:01Z"),
            ]
            (parent / "session.v1.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    {"type": "session", "version": 1, "id": "parent",
                     "cwd": "/work/parent"},
                    *inherited,
                ]) + "\n", encoding="utf-8")
            child_records = [
                {"type": "session", "version": 3, "id": "child",
                 "parentSession": "parent", "isSeeded": True,
                 "cwd": "/work/child"},
                user(20, "u1", "2026-09-01T01:10:00Z"),
                assistant(21, "a1", 100, "2026-09-01T01:10:01Z"),
                {"type": "session/end-seed", "seq": 22,
                 "time": "2026-09-01T01:10:02Z", "data": {"inherited": True}},
                user(23, "u2", "2026-09-01T01:11:00Z"),
                assistant(24, "a2", 300, "2026-09-01T01:11:01Z"),
            ]
            (child / "session.v3.jsonl").write_text(
                "\n".join(json.dumps(value) for value in child_records) + "\n",
                encoding="utf-8")

            # A stale V0 copy must not win over the current V3 generation.
            stale = sessions / "stale"
            stale.mkdir()
            (stale / "session.jsonl").write_text(
                json.dumps({"type": "session", "version": 0, "id": "same"})
                + "\n", encoding="utf-8")
            (stale / "session.v3.jsonl").write_text(
                json.dumps({"type": "session", "version": 3, "id": "same",
                            "isSeeded": False}) + "\n", encoding="utf-8")

            sources = dsh.discover({"config": {"data_root": str(sessions.parent)}})
            self.assertEqual(
                {item.path.name for item in sources},
                {"session.v1.jsonl", "session.v3.jsonl"},
            )
            skips = dsh.replay_skips(sources)
            child_source = next(item for item in sources
                                if item.path.parent.name == "child")
            self.assertEqual(skips[str(child_source.path)], 2)
            events = dsh.parse(
                child_source,
                skip_token_count=skips[str(child_source.path)],
            ).events
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].prompt_tokens, 300)

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
            with sqlite_connection(path) as connection:
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
                             (31, 2, 10, 33))

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
                              events[0].completion_tokens,
                              events[0].cache_read_tokens,
                              events[0].total_tokens),
                             ("wb-model", "project", 100, 20, 40, 120))

    def test_workbuddy_gives_copied_record_ids_a_global_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "projects" / "encoded"
            project.mkdir(parents=True)
            records = []
            for session_id, minute in (("session-a", "00"), ("session-b", "10")):
                records.append((project / f"{session_id}.jsonl", [
                    {
                        "id": "shared-request", "sessionId": session_id,
                        "type": "message", "role": "assistant",
                        "status": "completed", "timestamp": f"2026-08-24T01:{minute}:00Z",
                        "providerData": {
                            "requestModelId": "wb-model",
                            "usage": {"inputTokens": 10, "outputTokens": 2},
                        },
                    },
                ]))
            for path, values in records:
                path.write_text("\n".join(json.dumps(value) for value in values) + "\n",
                                encoding="utf-8")

            items = workbuddy.discover({"config": {"data_root": str(directory)}})
            events = [event for item in items for event in workbuddy.parse(item).events]
            self.assertEqual(len(events), 2)
            self.assertEqual({event.event_id for event in events}, {
                "workbuddy:record:shared-request",
            })

    def test_cursor_header_sentinel_protects_incremental_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.vscdb"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', 'Auth_0|token123')"
            )
            conn.commit()
            conn.close()

            item = UsageSource(path=db_path, key="cursor-auth")

            # Mock urlopen to return CSV with missing Model column
            with patch("app.services.agent_usage.adapters.cursor.urlopen") as mock_url:
                class MockResp:
                    def __init__(self, text):
                        self.text = text
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def read(self):
                        return self.text.encode("utf-8")

                # Missing Model column -> skipped=True with warning
                mock_url.return_value = MockResp("Date,Input (w/ Cache Write),Output Tokens\n2026-09-20T00:00:00Z,10,20\n")
                batch_res = cursor.parse(item)
                self.assertTrue(batch_res.skipped)
                self.assertTrue(any("导出表头与预期不符" in w for w in batch_res.warnings))
                self.assertEqual(len(batch_res.events), 0)

                # Valid columns -> parsed successfully
                mock_url.return_value = MockResp("Date,Model,Input (w/ Cache Write),Output Tokens\n2026-09-20T00:00:00Z,claude-3-5,10,20\n")
                valid_batch = cursor.parse(item)
                self.assertFalse(valid_batch.skipped)
                self.assertEqual(len(valid_batch.events), 1)
                self.assertEqual(valid_batch.events[0].model, "claude-3-5")

    def test_cola_reads_pi_compatible_session_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions_dir = root / "sessions" / "desktop-local"
            sessions_dir.mkdir(parents=True)
            session_file = sessions_dir / "session.jsonl"
            lines = [
                {"type": "session", "version": 3, "id": "cola-s1", "timestamp": "2026-09-10T01:00:00Z", "cwd": "/work/cola-demo"},
                {
                    "type": "message", "id": "m1", "parentId": None, "timestamp": "2026-09-10T01:00:01Z",
                    "message": {
                        "role": "assistant", "model": "claude-haiku-4-5-20251001",
                        "usage": {"input": 100, "output": 20, "cacheRead": 30, "cacheWrite": 10, "reasoning": 4},
                    },
                },
            ]
            session_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

            item = UsageSource(path=session_file, key="cola:desktop-local:session.jsonl", context={"sessions_root": str(root / "sessions")})
            batch_res = cola.parse(item)
            self.assertFalse(batch_res.skipped)
            self.assertEqual(len(batch_res.events), 1)
            ev = batch_res.events[0]
            self.assertEqual(ev.model, "claude-haiku-4-5-20251001")
            self.assertEqual(ev.project, "cola-demo")
            self.assertEqual(ev.cache_read_tokens, 30)
            self.assertEqual(ev.prompt_tokens, 140)
            self.assertEqual(ev.completion_tokens, 20)

    def test_qoder_and_qoder_cn_parse_ide_db_and_cli_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 1. Test IDE SQLite db
            db_path = root / "local.db"
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE chat_message (
                    id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, role TEXT,
                    token_info TEXT, model_info TEXT, gmt_create INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE chat_session (
                    session_id TEXT PRIMARY KEY, user_id TEXT, session_title TEXT,
                    project_uri TEXT, project_name TEXT, preferred_model_info TEXT
                )
            """)
            conn.execute(
                "INSERT INTO chat_session VALUES ('s1', 'u1', 'Title', 'file:///work/my-qoder-app', 'my-qoder-app', '')"
            )
            conn.execute(
                "INSERT INTO chat_message VALUES ('m1', 's1', 'r1', 'assistant', "
                "'{\"prompt_tokens\":18756,\"completion_tokens\":112,\"cached_tokens\":16334}', "
                "'{\"model_key\":\"auto\"}', 1788454780945)"
            )
            conn.commit()
            conn.close()

            item_ide = UsageSource(path=db_path, key="qoder:ide", context={"edition": "qoder", "source_type": "ide_db"})
            batch_ide = qoder.parse(item_ide)
            self.assertFalse(batch_ide.skipped)
            self.assertEqual(len(batch_ide.events), 1)
            ev_ide = batch_ide.events[0]
            self.assertEqual(ev_ide.model, "qoder-auto")  # routing tier normalized
            self.assertEqual(ev_ide.project, "my-qoder-app")
            self.assertEqual(ev_ide.prompt_tokens, 18756)
            self.assertEqual(ev_ide.cache_read_tokens, 16334)
            self.assertEqual(ev_ide.completion_tokens, 112)

            # Test qoder-cn edition on same db
            item_cn = UsageSource(path=db_path, key="qoder-cn:ide", context={"edition": "qoder-cn", "source_type": "ide_db"})
            batch_cn = qoder_cn.parse(item_cn)
            self.assertFalse(batch_cn.skipped)
            self.assertEqual(len(batch_cn.events), 1)
            self.assertEqual(batch_cn.events[0].model, "qoder-auto")

            # 2. Test CLI transcript (credit-only calls produce 0 events; token calls produce events)
            jsonl_path = root / "session.jsonl"
            lines = [
                # credit only call (0 tokens)
                {
                    "type": "assistant", "sessionId": "s-cli", "timestamp": "2026-09-03T16:43:25Z", "cwd": "/work/cli-proj",
                    "message": {"id": "c1", "model": "efficient", "usage": {"input_tokens": 0, "output_tokens": 0, "credits": 1.5}},
                },
                # real token call
                {
                    "type": "assistant", "sessionId": "s-cli", "timestamp": "2026-09-03T16:45:00Z", "cwd": "/work/cli-proj",
                    "message": {"id": "c2", "model": "qmodel_38max", "usage": {"input_tokens": 500, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens": 50}},
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

            item_cli = UsageSource(path=jsonl_path, key="qoder:cli", context={"edition": "qoder", "source_type": "transcript"})
            batch_cli = qoder.parse(item_cli)
            self.assertEqual(len(batch_cli.events), 1)
            ev_cli = batch_cli.events[0]
            self.assertEqual(ev_cli.model, "qmodel_38max")
            self.assertEqual(ev_cli.project, "cli-proj")
            self.assertEqual(ev_cli.prompt_tokens, 800)
            self.assertEqual(ev_cli.cache_read_tokens, 200)
            self.assertEqual(ev_cli.completion_tokens, 50)

    def test_qoder_path_environment_uses_canonical_names_with_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary_projects = Path(directory) / "primary-projects"
            legacy_projects = Path(directory) / "legacy-projects"
            primary_db = Path(directory) / "primary.db"
            legacy_db = Path(directory) / "legacy.db"
            with patch.dict(os.environ, {
                "QODER_PROJECTS_DIR": str(primary_projects),
                "VIBE_USAGE_QODER_PROJECTS": str(legacy_projects),
                "QODER_DB_PATH": str(primary_db),
                "VIBE_USAGE_QODER_DB": str(legacy_db),
                "QODERCN_PROJECTS_DIR": str(primary_projects),
                "VIBE_USAGE_QODER_CN_PROJECTS": str(legacy_projects),
                "QODERCN_DB_PATH": str(primary_db),
                "VIBE_USAGE_QODER_CN_DB": str(legacy_db),
            }):
                self.assertEqual(qoder.get_qoder_projects_dir(), primary_projects)
                self.assertEqual(qoder.get_qoder_db_path(), primary_db)
                self.assertEqual(qoder.get_qoder_projects_dir("qoder-cn"), primary_projects)
                self.assertEqual(qoder.get_qoder_db_path("qoder-cn"), primary_db)

            with patch.dict(os.environ, {
                "QODER_PROJECTS_DIR": "",
                "VIBE_USAGE_QODER_PROJECTS": str(legacy_projects),
                "QODER_DB_PATH": "",
                "VIBE_USAGE_QODER_DB": str(legacy_db),
            }):
                self.assertEqual(qoder.get_qoder_projects_dir(), legacy_projects)
                self.assertEqual(qoder.get_qoder_db_path(), legacy_db)

    def test_devin_schema_guard_and_snapshot_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "sessions.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE foo (x INTEGER)")
            conn.commit()
            conn.close()

            item = UsageSource(path=db_path)
            res_missing = devin.parse(item)
            self.assertTrue(res_missing.skipped)

            # Re-create with valid schema
            conn = sqlite3.connect(db_path)
            conn.execute("DROP TABLE foo")
            conn.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, working_directory TEXT NOT NULL, model TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE message_nodes (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    node_id INTEGER NOT NULL, chat_message TEXT NOT NULL, created_at INTEGER NOT NULL
                )
            """)
            conn.execute("INSERT INTO sessions VALUES ('devin-s1', '/work/devin-project', 'swe-2-high')")
            chat1 = {
                "message_id": "msg-1", "role": "assistant",
                "metadata": {
                    "created_at": "2026-09-18T10:00:00Z", "generation_model": "claude-3-7-sonnet",
                    "metrics": {"input_tokens": 120, "output_tokens": 35, "cache_read_tokens": 40, "cache_creation_tokens": 10},
                },
            }
            chat_dup = {
                "message_id": "msg-1", "role": "assistant",  # duplicate node
                "metadata": {
                    "created_at": "2026-09-18T10:00:00Z", "generation_model": "claude-3-7-sonnet",
                    "metrics": {"input_tokens": 120, "output_tokens": 35, "cache_read_tokens": 40, "cache_creation_tokens": 10},
                },
            }
            conn.execute("INSERT INTO message_nodes VALUES (1, 'devin-s1', 1, ?, 1789525600)", (json.dumps(chat1),))
            conn.execute("INSERT INTO message_nodes VALUES (2, 'devin-s1', 2, ?, 1789525600)", (json.dumps(chat_dup),))
            conn.commit()
            conn.close()

            res_valid = devin.parse(item)
            self.assertFalse(res_valid.skipped)
            self.assertEqual(len(res_valid.events), 1)  # duplicate message deduplicated
            ev = res_valid.events[0]
            self.assertEqual(ev.model, "claude-3-7-sonnet")
            self.assertEqual(ev.project, "devin-project")
            self.assertEqual(ev.prompt_tokens, 170)
            self.assertEqual(ev.cache_read_tokens, 40)
            self.assertEqual(ev.completion_tokens, 35)

    def test_codebuddy_parses_api_messages_and_routing_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proj_dir = root / "projects" / "private-tmp-demo-proj"
            proj_dir.mkdir(parents=True)
            session_file = proj_dir / "session-1.jsonl"
            lines = [
                {"id": "u1", "type": "message", "role": "user", "cwd": "/work/demo-proj", "timestamp": 1789525600000},
                {
                    "id": "a1", "type": "assistant", "timestamp": 1789525602000, "cwd": "/work/demo-proj",
                    "message": {
                        "model": None, "role": "assistant",
                        "usage": {"input_tokens": 100, "output_tokens": 47, "cache_read_input_tokens": 1344, "cache_creation_input_tokens": 10},
                    },
                    "providerData": {
                        "messageId": "msg-cb-1", "model": "claude-sonnet-4-6", "requestModelId": "auto",
                    },
                },
            ]
            session_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

            item = UsageSource(path=session_file, key="codebuddy:s1", context={"projects_dir": str(root / "projects")})
            batch_res = codebuddy.parse(item)
            self.assertFalse(batch_res.skipped)
            self.assertEqual(len(batch_res.events), 1)
            ev = batch_res.events[0]
            self.assertEqual(ev.model, "codebuddy-auto")
            self.assertEqual(ev.project, "demo-proj")
            self.assertEqual(ev.prompt_tokens, 1454)
            self.assertEqual(ev.cache_read_tokens, 1344)
            self.assertEqual(ev.completion_tokens, 47)

    def test_codearts_agent_recursive_tree_and_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "opencode.db"
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE message (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
                )
            """)
            conn.execute("INSERT INTO session VALUES ('parent-s1', NULL, '/work/codearts-proj')")
            conn.execute("INSERT INTO session VALUES ('child-s2', 'parent-s1', '/work/codearts-proj/sub')")

            msg_data = {
                "role": "assistant",
                "modelID": "GLM-5.2",
                "time": {"created": 1789525600000},
                "tokens": {"input": 80, "output": 25, "cache": {"read": 20, "write": 15}, "reasoning": 5},
            }
            conn.execute(
                "INSERT INTO message VALUES ('m-child', 'child-s2', 1789525600000, 1789525600000, ?)",
                (json.dumps(msg_data),)
            )
            conn.commit()
            conn.close()

            item = UsageSource(path=db_path, key="codearts-agent:opencode.db")
            batch_res = codearts_agent.parse(item)
            self.assertFalse(batch_res.skipped)
            self.assertEqual(len(batch_res.events), 1)
            ev = batch_res.events[0]
            self.assertEqual(ev.model, "GLM-5.2")
            self.assertEqual(ev.project, "codearts-proj")
            self.assertEqual(ev.session_id, "parent-s1")
            self.assertEqual(ev.prompt_tokens, 115)
            self.assertEqual(ev.cache_read_tokens, 20)
            self.assertEqual(ev.completion_tokens, 30)


if __name__ == "__main__":
    unittest.main()
