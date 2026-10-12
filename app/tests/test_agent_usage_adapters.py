"""Pure adapter/IR regression tests (no Flask application required)."""

from __future__ import annotations

import json
import os
import shutil
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
from app.tests.support import PROJECT_ROOT, sqlite_connection


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

    def test_amp_windows_probes_local_appdata_without_touching_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default = root / "xdg" / "amp" / "threads"
            default.mkdir(parents=True)
            (default / "T-default.json").write_text(
                json.dumps({"id": "default-thread"}), encoding="utf-8")
            local_app_data = root / "AppData" / "Local"
            native = local_app_data / "amp" / "threads"
            native.mkdir(parents=True)
            (native / "T-native.json").write_text(
                json.dumps({"id": "native-thread"}), encoding="utf-8")

            def names() -> set[str]:
                return {item.path.name for item in amp.discover({})}

            environment = {"AMP_DATA_DIR": "", "XDG_DATA_HOME": "",
                           "LOCALAPPDATA": str(local_app_data)}
            with patch.object(amp, "DEFAULT_PATH", default), \
                    patch.dict(os.environ, environment):
                with patch.object(amp.sys, "platform", "win32"):
                    self.assertEqual(names(), {"T-native.json"})
                with patch.object(amp.sys, "platform", "linux"):
                    # Only Windows probes %LOCALAPPDATA%.
                    self.assertEqual(names(), {"T-default.json"})
            # Without the native store the existing default root is unchanged.
            with patch.object(amp, "DEFAULT_PATH", default), patch.dict(
                    os.environ, {**environment,
                                 "LOCALAPPDATA": str(root / "Empty")}):
                with patch.object(amp.sys, "platform", "win32"):
                    self.assertEqual(names(), {"T-default.json"})
            # An empty LOCALAPPDATA is not a CWD-relative guess.
            with patch.object(amp, "DEFAULT_PATH", default), patch.dict(
                    os.environ, {**environment, "LOCALAPPDATA": ""}):
                with patch.object(amp.sys, "platform", "win32"):
                    self.assertEqual(names(), {"T-default.json"})

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

    def test_claude_estimated_duration_spans_tool_result_to_last_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "-work-project" / "session.jsonl"
            path.parent.mkdir(parents=True)

            def chain(kind, uuid, parent, ts):
                return {"parentUuid": parent, "type": kind, "uuid": uuid,
                        "timestamp": ts}

            def block(msg_id, uuid, parent, index, ts, stop_reason):
                return {"parentUuid": parent, "type": "assistant", "uuid": uuid,
                        "apiBlockIndex": index,
                        "message": {"id": msg_id, "model": "claude-opus-4-8",
                                    "usage": {"input_tokens": 10,
                                              "output_tokens": 900},
                                    "stop_reason": stop_reason},
                        "timestamp": ts}

            lines = [
                # 起点:工具结果
                chain("user", "c1", None, "2026-06-07T13:00:00.000Z"),
                chain("attachment", "c2", "c1", "2026-06-07T13:00:00.003Z"),
                # 回复开始后才补记的 attachment,时间戳比第一块还晚,不能当起点
                chain("attachment", "c3", "c2", "2026-06-07T13:00:05.001Z"),
                block("msg-t", "c4", "c3", 0, "2026-06-07T13:00:05.000Z", "tool_use"),
                block("msg-t", "c5", "c4", 1, "2026-06-07T13:00:07.000Z", "tool_use"),
                # 并行工具调用:前一个工具的结果插在同一条回复的两块中间
                chain("user", "c6", "c5", "2026-06-07T13:00:08.000Z"),
                block("msg-t", "c7", "c6", 2, "2026-06-07T13:00:12.500Z", "end_turn"),
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n",
                            encoding="utf-8")

            items = claude_code.discover({"config": {"data_root": str(root)}})
            parsed = claude_code.parse(items[0])

            self.assertEqual(len(parsed.events), 1)
            self.assertEqual(parsed.events[0].duration_ms, 12_500)

    def test_claude_duration_needs_stop_reason_first_block_and_plausible_span(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "projects" / "-work-project" / "session.jsonl"
            path.parent.mkdir(parents=True)

            def line(kind, uuid, parent, ts):
                return {"parentUuid": parent, "type": kind, "uuid": uuid,
                        "timestamp": ts}

            def block(msg_id, uuid, parent, index, ts, stop_reason):
                return {"parentUuid": parent, "type": "assistant", "uuid": uuid,
                        "apiBlockIndex": index,
                        "message": {"id": msg_id, "model": "claude-opus-4-8",
                                    "usage": {"input_tokens": 10,
                                              "output_tokens": 900},
                                    "stop_reason": stop_reason},
                        "timestamp": ts}

            lines = [
                line("user", "d1", None, "2026-06-07T13:00:00.000Z"),
                # 没有 stop_reason:回复没写完,不估
                block("msg-open", "d2", "d1", 0,
                      "2026-06-07T13:00:09.000Z", None),
                # 父行不在文件里:起点沿链找不到
                block("msg-orphan", "d3", "missing", 0,
                      "2026-06-07T13:00:20.000Z", "end_turn"),
                # 起点和结束挨得太近,不像一次真实请求
                line("user", "d4", None, "2026-06-07T13:01:00.000Z"),
                block("msg-instant", "d5", "d4", 0,
                      "2026-06-07T13:01:00.020Z", "end_turn"),
                # 超过一小时:中间多半夹了休眠或重试等待
                line("user", "d6", None, "2026-06-07T15:00:00.000Z"),
                block("msg-sleep", "d7", "d6", 0,
                      "2026-06-07T16:00:00.001Z", "end_turn"),
                # 最先看到的是第 2 块,起点会取晚,宁可不估
                line("user", "d8", None, "2026-06-07T17:00:00.000Z"),
                block("msg-late", "d9", "d8", 2,
                      "2026-06-07T17:00:09.000Z", "end_turn"),
            ]
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n",
                            encoding="utf-8")

            items = claude_code.discover({"config": {"data_root": str(root)}})
            parsed = claude_code.parse(items[0])

            durations = {event.event_id: event.duration_ms
                         for event in parsed.events}
            self.assertEqual(durations, {
                "claude-code:call:msg-open": 0,
                "claude-code:call:msg-orphan": 0,
                "claude-code:call:msg-instant": 0,
                "claude-code:call:msg-sleep": 0,
                "claude-code:call:msg-late": 0,
            })

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

    # ── antigravity:protobuf 夹具(与适配器解码的线上格式一致) ──

    @staticmethod
    def _pb_varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    @classmethod
    def _pb_field(cls, number: int, wire: int) -> bytes:
        return cls._pb_varint((number << 3) | wire)

    @classmethod
    def _pb_varint_field(cls, number: int, value: int) -> bytes:
        return cls._pb_field(number, 0) + cls._pb_varint(value)

    @classmethod
    def _pb_bytes_field(cls, number: int, payload: bytes) -> bytes:
        return cls._pb_field(number, 2) + cls._pb_varint(len(payload)) + payload

    @classmethod
    def _pb_string_field(cls, number: int, text: str) -> bytes:
        return cls._pb_bytes_field(number, text.encode("utf-8"))

    @classmethod
    def _antigravity_usage_blob(cls, *, model_id=None, input=None, total_output=None,
                                cache_creation=None, cache_read=None, thinking=None,
                                visible=None, message_id=None, response_id=None,
                                provider_message_id=None) -> bytes:
        """一行 ModelUsage 消息:字段 3 是输出总量,字段 10 是可见输出。"""
        parts = b""
        for number, value in ((1, model_id), (2, input), (3, total_output),
                              (4, cache_creation), (5, cache_read), (9, thinking),
                              (10, visible)):
            if value is not None:
                parts += cls._pb_varint_field(number, value)
        for number, value in ((7, message_id), (11, response_id),
                              (12, provider_message_id)):
            if value is not None:
                parts += cls._pb_string_field(number, value)
        return parts

    @classmethod
    def _antigravity_gen_blob(cls, *, seconds=None, response_model=None,
                              display_name=None, retries=(), **usage_fields) -> bytes:
        """一行 gen_metadata:chatModel(1) 含 usage(4)、重试(17)、时钟(9.4.1)。"""
        usage = cls._antigravity_usage_blob(**usage_fields)
        chat = b""
        if usage:
            chat += cls._pb_bytes_field(4, usage)
        for retry in retries:
            chat += cls._pb_bytes_field(17, cls._pb_bytes_field(2, retry))
        if seconds is not None:
            chat += cls._pb_bytes_field(
                9, cls._pb_bytes_field(4, cls._pb_varint_field(1, seconds)))
        if response_model is not None:
            chat += cls._pb_string_field(19, response_model)
        if display_name is not None:
            chat += cls._pb_string_field(21, display_name)
        return cls._pb_bytes_field(1, chat)

    @classmethod
    def _antigravity_step_blob(cls, *, source=None, seconds=None,
                               completion_seconds=None, usage=None,
                               model_name=None, model_id=None, retries=()) -> bytes:
        """一行 steps.metadata:时钟 1.1(或 8.1)、来源(3)、用量(9/28)、模型(24)。"""
        parts = b""
        if seconds is not None:
            parts += cls._pb_bytes_field(1, cls._pb_varint_field(1, seconds))
        if source is not None:
            parts += cls._pb_varint_field(3, source)
        if completion_seconds is not None:
            parts += cls._pb_bytes_field(8, cls._pb_varint_field(1, completion_seconds))
        if usage is not None:
            parts += cls._pb_bytes_field(9, usage)
        for retry in retries:
            parts += cls._pb_bytes_field(28, cls._pb_bytes_field(2, retry))
        if model_name is not None or model_id is not None:
            info = b""
            if model_id is not None:
                info += cls._pb_varint_field(1, model_id)
            if model_name is not None:
                info += cls._pb_string_field(12, model_name)
            parts += cls._pb_bytes_field(24, info)
        return parts

    @staticmethod
    def _antigravity_db(path: Path, gen_rows=(), step_rows=()) -> None:
        with sqlite_connection(path) as connection:
            connection.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
            connection.execute("CREATE TABLE steps (idx INTEGER, metadata BLOB)")
            for idx, blob in gen_rows:
                connection.execute("INSERT INTO gen_metadata VALUES (?,?)", (idx, blob))
            for idx, blob in step_rows:
                connection.execute("INSERT INTO steps VALUES (?,?)", (idx, blob))

    @staticmethod
    def _antigravity_conversations(directory: str) -> Path:
        conversations = Path(directory) / "conversations"
        conversations.mkdir()
        return conversations

    def _parse_antigravity_conversations(self, conversations: Path):
        with patch.dict(os.environ, {
                "VIBE_USAGE_ANTIGRAVITY_DIRS": str(conversations)}):
            items = antigravity.discover({})
            self.assertEqual(len(items), 1)
            return antigravity.parse(items[0])

    def test_antigravity_gen_metadata_splits_thinking_from_total_output(self) -> None:
        # 字段 3 是输出总量(可见 + thinking),字段 10 是可见部分;thinking 必须
        # 离开 output,否则宿主把 reasoning 折回 completion 时会双计。
        record = antigravity._parse_gen_metadata_blob(
            self._antigravity_gen_blob(
                input=5528, total_output=192, visible=50, cache_read=24481,
                thinking=142, response_id="RESP_1", seconds=1783484082,
                response_model="gemini-3-flash-a",
                display_name="Gemini 3.5 Flash (High)"))
        self.assertEqual(record["input"], 5528)
        self.assertEqual(record["total_output"], 192)
        self.assertEqual(record["output"], 50)
        self.assertEqual(record["thinking"], 142)
        self.assertEqual(record["cache_read"], 24481)
        self.assertEqual(record["response_id"], "RESP_1")
        self.assertEqual(record["response_model"], "gemini-3-flash-a")
        self.assertEqual(record["display_name"], "Gemini 3.5 Flash (High)")
        self.assertEqual(record["timestamp"], "2026-07-08T04:14:42Z")

        # 没有 visible 字段时:总量减去 thinking 就是可见输出。
        split = antigravity._parse_gen_metadata_blob(
            self._antigravity_gen_blob(
                input=10, total_output=100, thinking=30, response_id="R"))
        self.assertEqual(split["output"], 70)
        self.assertEqual(split["thinking"], 30)
        self.assertEqual(split["output"] + split["thinking"], split["total_output"])

        # 没有 token 用量的行(错误/规划占位)不产生记录。
        self.assertIsNone(antigravity._parse_gen_metadata_blob(
            self._antigravity_gen_blob(
                seconds=1783484000, response_model="gemini-default")))

    def test_antigravity_step_rows_scan_usage_timestamps_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cascade-steps.db"
            usage = self._antigravity_usage_blob(input=500, total_output=60, thinking=50)
            self._antigravity_db(db, step_rows=[
                (1, self._antigravity_step_blob(source=4, seconds=1783508701)),
                (2, self._antigravity_step_blob(source=2, seconds=1783508703,
                                                usage=usage)),
                # 工具步骤(来源 5)不产生事件,但它的时钟仍进入 idx → 时间戳表。
                (3, self._antigravity_step_blob(source=5, seconds=1783508705)),
                # 1.1 缺失、只有 8.1 的步骤仍然提供时钟。
                (4, self._antigravity_step_blob(source=2,
                                                completion_seconds=1783508707)),
            ])
            rows = antigravity._read_step_rows(db, strict=True)

        self.assertEqual([event["role"] for event in rows["events"]],
                         ["user", "assistant", "assistant"])
        self.assertEqual(rows["events"][1]["timestamp"], "2026-07-08T11:05:03Z")
        self.assertEqual(len(rows["timestamps"]), 4)
        self.assertEqual(rows["timestamps"][3], "2026-07-08T11:05:05Z")
        self.assertEqual(rows["timestamps"][4], "2026-07-08T11:05:07Z")
        self.assertEqual(len(rows["usages"]), 1)
        self.assertEqual(rows["usages"][0]["usage"]["input"], 500)
        self.assertEqual(rows["usages"][0]["timestamp"], "2026-07-08T11:05:03Z")

    def test_antigravity_parse_counts_usage_kept_only_on_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            usage = self._antigravity_usage_blob(
                model_id=1318, input=900, total_output=120, cache_creation=700,
                cache_read=300, thinking=80, visible=40,
                response_id="RESP_STEP_ONLY")
            self._antigravity_db(conversations / "step-usage.db", step_rows=[
                (1, self._antigravity_step_blob(source=4, seconds=1783508701)),
                (2, self._antigravity_step_blob(
                    source=2, seconds=1783508703, usage=usage,
                    model_name="Gemini 3.8 Flash (High)")),
            ])
            parsed = self._parse_antigravity_conversations(conversations)

        # 完成量 = 可见 40 + thinking 80 = 字段 3 的总量,thinking 只计一次;
        # cache-creation 按仓库惯例折入 input。
        self.assert_one_event(
            parsed, model="Gemini 3.8 Flash (High)",
            prompt=1900, completion=120, cache=300, total=2020)
        self.assertEqual(parsed.events[0].requested_at, "2026-07-08T11:05:03Z")

    def test_antigravity_parse_counts_a_call_once_when_both_tables_carry_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            shared = self._antigravity_usage_blob(
                input=900, total_output=120, cache_read=300,
                response_id="RESP_SHARED")
            self._antigravity_db(
                conversations / "both-tables.db",
                gen_rows=[(1, self._antigravity_gen_blob(
                    input=900, total_output=120, cache_read=300,
                    response_id="RESP_SHARED", seconds=1783508703,
                    display_name="Gemini 3.8 Flash", retries=[shared]))],
                step_rows=[
                    (1, self._antigravity_step_blob(source=4, seconds=1783508701)),
                    (2, self._antigravity_step_blob(source=2, seconds=1783508703,
                                                    usage=shared)),
                    # 没有身份键的 step 记录旁边有 generator 用量时必须丢弃,
                    # 否则同一轮次会被计两次。
                    (3, self._antigravity_step_blob(
                        source=2, seconds=1783508710,
                        usage=self._antigravity_usage_blob(
                            input=50, total_output=5))),
                ])
            parsed = self._parse_antigravity_conversations(conversations)

        self.assert_one_event(
            parsed, model="Gemini 3.8 Flash",
            prompt=1200, completion=120, cache=300, total=1320)

    def test_antigravity_model_fallback_cascades_to_the_last_named_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            self._antigravity_db(conversations / "model-fallback.db", step_rows=[
                (1, self._antigravity_step_blob(
                    source=2, seconds=1783508703,
                    model_name="Gemini 3.8 Flash (High)",
                    usage=self._antigravity_usage_blob(
                        model_id=1318, input=100, total_output=10,
                        response_id="RESP_BY_ID"))),
                # 无名称、无 id、无身份的步骤:整条级联没有 generator 用量时
                # 保留,并承接最近一个具名模型。
                (2, self._antigravity_step_blob(
                    source=2, seconds=1783508800,
                    usage=self._antigravity_usage_blob(input=200, total_output=20))),
                # 只有数字 id 的步骤走 id 表。
                (3, self._antigravity_step_blob(
                    source=2, seconds=1783508900,
                    usage=self._antigravity_usage_blob(
                        model_id=1318, input=300, response_id="RESP_ID_ONLY"))),
            ])
            parsed = self._parse_antigravity_conversations(conversations)

        by_model = {}
        for event in parsed.events:
            by_model[event.model] = by_model.get(event.model, 0) + event.prompt_tokens
        self.assertEqual(by_model, {
            "Gemini 3.8 Flash (High)": 300, "gemini-3.8-flash-high": 300})

    def test_antigravity_step_retry_entries_merge_into_their_parent_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            # 重试条目重复的是所属调用的用量(同一 response id);真正多出来
            # 的一次尝试保留自己的 id,单独计数。
            shared = self._antigravity_usage_blob(
                input=100, total_output=10, response_id="RESP_STEP")
            extra_attempt = self._antigravity_usage_blob(
                input=50, total_output=5, response_id="RESP_ATTEMPT")
            self._antigravity_db(conversations / "retries.db", step_rows=[
                (1, self._antigravity_step_blob(
                    source=2, seconds=1783508703, usage=shared,
                    retries=[shared, extra_attempt])),
            ])
            parsed = self._parse_antigravity_conversations(conversations)

        self.assertFalse(parsed.skipped)
        by_id = {event.event_id: event for event in parsed.events}
        self.assertEqual(set(by_id), {
            "antigravity:response:RESP_STEP",
            "antigravity:response:RESP_ATTEMPT"})
        self.assertEqual(by_id["antigravity:response:RESP_STEP"].prompt_tokens, 100)
        self.assertEqual(by_id["antigravity:response:RESP_ATTEMPT"].prompt_tokens, 50)

    def test_antigravity_retry_entries_join_by_identity_like_ref(self) -> None:
        # 重试副本带父调用的身份键:并入父调用,只计一次。
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            self._antigravity_db(conversations / "retry-shared.db", gen_rows=[(
                1, self._antigravity_gen_blob(
                    input=100, total_output=10, response_id="RESP_MAIN",
                    seconds=1783508703,
                    retries=[self._antigravity_usage_blob(
                        input=100, total_output=10, response_id="RESP_MAIN")]))])
            shared = self._parse_antigravity_conversations(conversations)
        self.assert_one_event(shared, model="unknown", prompt=100,
                              completion=10, cache=0, total=110)
        self.assertEqual(shared.events[0].event_id,
                         "antigravity:response:RESP_MAIN")

        # 没有身份键的重试条目没有可归并的目标,自成一个调用(ref HEAD 的
        # 归并同样按身份键进行,只有共享 id 的副本才折叠)。
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            self._antigravity_db(conversations / "retry-anonymous.db", gen_rows=[(
                1, self._antigravity_gen_blob(
                    input=100, total_output=10, response_id="RESP_MAIN",
                    seconds=1783508703,
                    retries=[self._antigravity_usage_blob(
                        input=100, total_output=10)]))])
            anonymous = self._parse_antigravity_conversations(conversations)
        self.assertEqual([event.prompt_tokens for event in anonymous.events],
                         [100, 100])

    def test_antigravity_event_id_is_stable_when_the_richer_copy_flips(self) -> None:
        # 同一次调用(共享 message id)的 gen_metadata 副本与 step 副本,
        # 谁更丰富取决于各表当前写到的进度。event_id 取身份键并集里最稳定
        # 的一条,不能随胜出副本变化,否则 ALWAYS_SCAN 下会为同一次调用
        # 插入第二行。
        def parsed_event(step_input: int):
            with tempfile.TemporaryDirectory() as directory:
                conversations = self._antigravity_conversations(directory)
                self._antigravity_db(
                    conversations / "winner-flip.db",
                    gen_rows=[(1, self._antigravity_gen_blob(
                        input=100, total_output=10, message_id="TURN1",
                        seconds=1783508703))],
                    step_rows=[(2, self._antigravity_step_blob(
                        source=2, seconds=1783508704,
                        usage=self._antigravity_usage_blob(
                            input=step_input, total_output=10,
                            message_id="TURN1")))])
                parsed = self._parse_antigravity_conversations(conversations)
            self.assertEqual(len(parsed.events), 1)
            return parsed.events[0]

        gen_wins = parsed_event(90)
        step_wins = parsed_event(200)
        self.assertEqual(gen_wins.event_id, "antigravity:message:TURN1")
        self.assertEqual(step_wins.event_id, gen_wins.event_id)
        self.assertEqual((gen_wins.prompt_tokens, step_wins.prompt_tokens),
                         (100, 200))

    def test_antigravity_out_of_range_step_clock_does_not_abort_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            db = conversations / "overflow.db"
            self._antigravity_db(db, step_rows=[
                # 损坏/超界的 varint 只会让这一行没有时钟。
                (1, self._antigravity_step_blob(
                    source=2, seconds=2 ** 200,
                    usage=self._antigravity_usage_blob(
                        input=100, total_output=10, response_id="RESP_BAD_CLOCK"))),
                (2, self._antigravity_step_blob(
                    source=2, seconds=1783508703,
                    usage=self._antigravity_usage_blob(
                        input=7, total_output=1, response_id="RESP_GOOD_CLOCK"))),
            ])
            rows = antigravity._read_step_rows(db, strict=True)
            parsed = self._parse_antigravity_conversations(conversations)

        self.assertEqual(rows["timestamps"], {2: "2026-07-08T11:05:03Z"})
        self.assertFalse(parsed.skipped)
        self.assertEqual([event.prompt_tokens for event in parsed.events], [7])

    def test_antigravity_model_from_record_prefers_display_name_over_slug(self) -> None:
        self.assertEqual(
            antigravity._model_from_record({
                "display_name": "Gemini 3.5 Flash (Medium)",
                "response_model": "gemini-default"}),
            "Gemini 3.5 Flash (Medium)")
        self.assertEqual(
            antigravity._model_from_record({"response_model": "gemini-3-pro-high"}),
            "gemini-3-pro")
        self.assertEqual(
            antigravity._model_from_record(
                {"model_name": "claude-sonnet-4-6-thinking"}),
            "claude-sonnet-4-6")
        self.assertEqual(
            antigravity._model_from_record({"model_id": 1318}),
            "gemini-3.8-flash-high")
        self.assertEqual(antigravity._model_from_record({}), "unknown")

    def test_antigravity_gemini37_rows_take_the_step_clock_by_idx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            conversations = self._antigravity_conversations(directory)
            self._antigravity_db(
                conversations / "cascade-37.db",
                gen_rows=[(12, self._antigravity_gen_blob(
                    input=5000, total_output=80, cache_read=40000, thinking=12,
                    response_id="RESP_37_JOIN",
                    response_model="gemini-3.7-flash-safety-le"))],
                step_rows=[(12, self._antigravity_step_blob(
                    source=5, seconds=1787350732))])
            parsed = self._parse_antigravity_conversations(conversations)

        self.assert_one_event(
            parsed, model="gemini-3.7-flash-safety-le",
            prompt=45000, completion=80, cache=40000, total=45080)
        self.assertEqual(parsed.events[0].requested_at, "2026-08-21T22:18:52Z")

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
                        "cacheCreationTokens": 3,
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

            # RPC 序列化的是与 .db 行相同的 protobuf:outputTokens(字段 3)是
            # 总量,thinking 只是其中一部分 —— 完成量仍是 5,不是 5 + 1;
            # cache-creation 与 .db 路径同口径折入 prompt。
            event = parsed.events[0]
            self.assertEqual((event.model, event.project,
                              event.prompt_tokens, event.completion_tokens,
                              event.cache_read_tokens, event.total_tokens),
                             ("gemini-3-pro", "project-legacy", 105, 5, 2, 110))

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

    # ---- mcode 多数据根(默认布局探测、重定位变量、跨库去重) ----

    MCODE_SCHEMA = """
        CREATE TABLE local_runtime_sessions (
            session_id TEXT PRIMARY KEY, workspace_dir TEXT,
            project_workspace_dir TEXT
        );
        CREATE TABLE local_runtime_token_usage (
            session_id TEXT, model TEXT, ts INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, raw TEXT
        );
    """

    # 关闭 mcode 的全部定位变量:空值等于未设置。
    MCODE_NO_LOCATORS = {
        "VIBE_USAGE_MCODE_DB": "", "MCODE_HOME": "",
        "MINIMAX_DATA_DIR": "", "MAVIS_DATA_DIR": "",
    }

    @staticmethod
    def _mcode_store_path(root: Path) -> Path:
        return root / "v2" / "sqlite" / "runtime-state.sqlite"

    @classmethod
    def _write_mcode_db(cls, path: Path, *, sessions=(), tokens=(),
                        schema: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_connection(path) as connection:
            connection.executescript(schema or cls.MCODE_SCHEMA)
            for session in sessions:
                connection.execute(
                    "INSERT INTO local_runtime_sessions VALUES(?,?,?)", session)
            for token in tokens:
                connection.execute(
                    "INSERT INTO local_runtime_token_usage "
                    "VALUES(?,?,?,?,?,?,?,?,NULL)", token)
        return path

    @staticmethod
    def _mcode_totals(parsed) -> dict:
        totals: dict[str, list[int]] = {}
        for event in parsed.events:
            bucket = totals.setdefault(event.project, [0, 0, 0])
            bucket[0] += event.prompt_tokens
            bucket[1] += event.completion_tokens
            bucket[2] += event.total_tokens
        return totals

    def test_mcode_reads_profiles_and_legacy_roots_and_collapses_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            primary = self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                sessions=[("s1", "/work/primary", None)],
                tokens=[
                    ("s1", "mcode-model", 1782720000000, 10, 2, 0, 0, 0),
                    # 同一库内内容重复的行是账本本身的一部分:保留两行。
                    ("s1", "mcode-model", 1782720000000, 10, 2, 0, 0, 0),
                    ("s1", "mcode-model", 1782720060000, 5, 0, 0, 2, 0),
                ],
            )
            legacy = self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-code"),
                sessions=[("s9", "/work/old", None)],
                tokens=[
                    ("s9", "mcode-model", 1782720120000, 3, 1, 0, 0, 0),
                    # 迁移复制来的行:内容与主库第 3 行相同而 rowid 不同,而且
                    # 这个库里没有它的会话行;账本字段一致就要折叠,联表字段
                    # 缺失不能让同一笔用量再计一次。
                    ("s1", "mcode-model", 1782720060000, 5, 0, 0, 2, 0),
                ],
            )
            renamed = self._write_mcode_db(
                self._mcode_store_path(home / ".mavis-old"),
                sessions=[("s3", "/work/mavis", None)],
                tokens=[("s3", "mcode-model", 1782720240000, 4, 0, 0, 0, 0)],
            )
            profile = self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-work"),
                sessions=[("s2", "/work/profile", None)],
                tokens=[("s2", "mcode-model", 1782720180000, 7, 1, 0, 0, 1)],
            )
            # ~/.mavis 是指向 ~/.minimax 的兼容符号链接:同一份账本不能读两次。
            os.symlink(home / ".minimax", home / ".mavis")

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                items = mcode.discover({})
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].path, primary)
                self.assertEqual(
                    tuple(items[0].context["stores"]),
                    tuple(str(path) for path in (
                        primary, legacy, renamed, profile)))
                parsed = mcode.parse(items[0])

            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 6)
            self.assertEqual(len({event.event_id for event in parsed.events}), 6)
            self.assertEqual(self._mcode_totals(parsed), {
                "primary": [27, 4, 31],
                "old": [3, 1, 4],
                "profile": [8, 1, 9],
                "mavis": [4, 0, 4],
            })

    def test_mcode_deduplicates_duplicate_session_join_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # 没有唯一约束的 sessions 表里同一 session_id 的两行会让 LEFT
            # JOIN 把一条账本行扇出两份。
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                schema=self.MCODE_SCHEMA.replace(
                    "session_id TEXT PRIMARY KEY", "session_id TEXT"),
                sessions=[("s1", "/work/first", None),
                          ("s1", "/work/second", None)],
                tokens=[("s1", "mcode-model", 1782720000000, 10, 2, 0, 0, 0)])

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                parsed = mcode.parse(mcode.discover({})[0])

            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            self.assertEqual(self._mcode_totals(parsed), {"first": [10, 2, 12]})

    def test_mcode_event_ids_survive_new_stores_and_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = self._mcode_store_path(home / ".minimax-code")
            self._write_mcode_db(
                legacy,
                sessions=[("s1", "/work/legacy", None)],
                tokens=[("s1", "mcode-model", 1782720000000, 3, 1, 0, 0, 0)])
            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                first = mcode.parse(mcode.discover({})[0])
                self.assertEqual(len(first.events), 1)
                legacy_id = first.events[0].event_id

                # 主数据根出现,里面是迁移复制来的同一行(rowid 不同)外加
                # 一行新用量:副本的 event_id 必须与首次导入时完全一致,
                # 否则永久收据挡不住重复入库。
                self._write_mcode_db(
                    self._mcode_store_path(home / ".minimax"),
                    sessions=[("s9", "/work/other", None),
                              ("s1", "/work/legacy", None)],
                    tokens=[
                        ("s9", "mcode-model", 1782720000000, 9, 9, 0, 0, 0),
                        ("s1", "mcode-model", 1782720000000, 3, 1, 0, 0, 0),
                    ])
                second = mcode.parse(mcode.discover({})[0])

            self.assertFalse(second.skipped)
            self.assertEqual(len(second.events), 2)
            self.assertIn(legacy_id, {event.event_id for event in second.events})
            self.assertEqual(self._mcode_totals(second), {
                "legacy": [3, 1, 4],
                "other": [9, 9, 18],
            })

    def test_mcode_data_root_config_accepts_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".minimax"
            path = self._write_mcode_db(
                self._mcode_store_path(home),
                tokens=[("s1", "mcode-model", 1782720000000, 1, 1, 0, 0, 0)])

            item = mcode.discover({"config": {"data_root": str(home)}})[0]
            self.assertEqual(item.path, path)
            self.assertEqual(mcode.parse(item).events[0].model, "mcode-model")

    def test_mcode_relocation_env_wins_over_default_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "relocated"
            relocated = self._write_mcode_db(
                self._mcode_store_path(root),
                tokens=[("s1", "mcode-model", 1782720000000, 1, 1, 0, 0, 0)])
            # 默认布局里的库同时存在:显式重定位必须完全取代探测。
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                tokens=[("s2", "mcode-model", 1782720000000, 2, 2, 0, 0, 0)])
            direct = self._write_mcode_db(home / "direct.sqlite",
                                          tokens=[("s3", "mcode-model", 1782720000000,
                                                   3, 3, 0, 0, 0)])

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                for name in ("MCODE_HOME", "MINIMAX_DATA_DIR",
                             "MAVIS_DATA_DIR"):
                    with patch.dict(os.environ, {name: str(root)}):
                        item = mcode.discover({})[0]
                        self.assertEqual(item.path, relocated)
                        self.assertEqual(
                            tuple(item.context["stores"]), (str(relocated),))
                # CLI 自己的变量顺序:MCODE_HOME 优先于两个公开重定位变量。
                with patch.dict(os.environ, {
                        "MCODE_HOME": str(root),
                        "MINIMAX_DATA_DIR": str(root / "other"),
                        "MAVIS_DATA_DIR": str(root / "other")}):
                    self.assertEqual(mcode.discover({})[0].path, relocated)
                with patch.dict(os.environ, {
                        "MINIMAX_DATA_DIR": str(root),
                        "MAVIS_DATA_DIR": str(root / "other")}):
                    self.assertEqual(mcode.discover({})[0].path, relocated)
                with patch.dict(os.environ, {
                        "VIBE_USAGE_MCODE_DB": str(direct),
                        "MINIMAX_DATA_DIR": str(root)}):
                    item = mcode.discover({})[0]
                    self.assertEqual(item.path, direct)
                    self.assertEqual(
                        tuple(item.context["stores"]), (str(direct),))
                for name in ("MCODE_HOME", "MINIMAX_DATA_DIR",
                             "MAVIS_DATA_DIR"):
                    with patch.dict(os.environ, {name: "relative/store"}):
                        with self.assertRaises(ValueError):
                            mcode.discover({})

    def test_mcode_errors_in_extra_stores_keep_the_live_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            primary = self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                sessions=[("s1", "/work/live", None)],
                tokens=[("s1", "mcode-model", 1782720000000, 4, 2, 0, 0, 0)])
            # 构建不认识的剖面库:结构不兼容,只能告警。
            broken = self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-broken"),
                schema="CREATE TABLE local_runtime_token_usage (session_id TEXT);")
            # 剖面根里放着不是 SQLite 的文件:无法读取,只能告警。
            corrupt = self._mcode_store_path(home / ".mavis-corrupt")
            corrupt.parent.mkdir(parents=True, exist_ok=True)
            corrupt.write_bytes(b"not a sqlite database")

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                item = mcode.discover({})[0]
                parsed = mcode.parse(item)

            self.assertTrue(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            self.assertEqual(parsed.events[0].project, "live")
            self.assertEqual(self._mcode_totals(parsed), {"live": [4, 2, 6]})
            self.assertEqual(len(parsed.warnings), 2)
            self.assertTrue(any(str(broken) in warning and "结构不兼容" in warning
                                for warning in parsed.warnings))
            self.assertTrue(any(str(corrupt) in warning and "无法读取" in warning
                                for warning in parsed.warnings))
            self.assertNotIn(str(primary), "".join(parsed.warnings))

    def test_mcode_primary_store_failure_does_not_read_extra_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                schema="CREATE TABLE local_runtime_token_usage (session_id TEXT);")
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-code"),
                tokens=[("s9", "mcode-model", 1782720000000, 9, 9, 0, 0, 0)])

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                parsed = mcode.parse(mcode.discover({})[0])

            self.assertTrue(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertEqual(
                parsed.warnings, ("mcode: runtime-state 数据库结构不兼容",))

    def test_mcode_import_once_counts_every_store_once(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        from app.db.schema_upgrade import ensure_local_databases
        from app.services.agent_usage.importer import import_once

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(PROJECT_ROOT / "schema", root / "schema")
            data = root / "data"
            data.mkdir()
            proxy_path = data / "token-board.db"
            ensure_local_databases(
                str(proxy_path), str(data / "dashboard.db"), root / "schema")
            home = root / "home"
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax"),
                sessions=[("s1", "/work/primary", None)],
                tokens=[
                    ("s1", "mcode-model", 1782720000000, 10, 2, 0, 1, 0),
                    ("s1", "mcode-model", 1782720060000, 4, 0, 0, 0, 0),
                ],
            )
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-code"),
                sessions=[("s1", "/work/primary", None)],
                tokens=[
                    ("s1", "mcode-model", 1782720060000, 4, 0, 0, 0, 0),
                    ("s1", "mcode-model", 1782720120000, 3, 1, 0, 0, 0),
                ],
            )
            database = ProxyDatabase(str(proxy_path))
            database.create_agent_software(
                {"name": "mcode-agent", "agent_kind": "mcode"})

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                self.assertEqual(import_once(database), 3)
                # 第二次运行幂等:复制的行与已导入的行都不重复计数。
                self.assertEqual(import_once(database), 0)

            with sqlite_connection(proxy_path) as connection:
                rows = connection.execute(
                    "SELECT model,prompt_tokens,completion_tokens,"
                    "cache_read_tokens,total_tokens "
                    "FROM request_log WHERE event_id LIKE 'mcode:%' "
                    "ORDER BY requested_at").fetchall()
            self.assertEqual(
                [tuple(str(value) if index == 0 else int(value)
                       for index, value in enumerate(row))
                 for row in rows],
                [("mcode-model", 11, 2, 1, 13),
                 ("mcode-model", 4, 0, 0, 4),
                 ("mcode-model", 3, 1, 0, 4)])

    def test_mcode_import_once_is_stable_when_a_copied_store_appears(self) -> None:
        from app.db.proxy_db import ProxyDatabase
        from app.db.schema_upgrade import ensure_local_databases
        from app.services.agent_usage.importer import import_once

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(PROJECT_ROOT / "schema", root / "schema")
            data = root / "data"
            data.mkdir()
            proxy_path = data / "token-board.db"
            ensure_local_databases(
                str(proxy_path), str(data / "dashboard.db"), root / "schema")
            home = root / "home"
            self._write_mcode_db(
                self._mcode_store_path(home / ".minimax-code"),
                sessions=[("s1", "/work/primary", None)],
                tokens=[
                    ("s1", "mcode-model", 1782720000000, 10, 2, 0, 1, 0),
                    ("s1", "mcode-model", 1782720060000, 4, 0, 0, 0, 0),
                ],
            )
            database = ProxyDatabase(str(proxy_path))
            database.create_agent_software(
                {"name": "mcode-agent", "agent_kind": "mcode"})

            def imported_rows() -> tuple:
                with sqlite_connection(proxy_path) as connection:
                    row = connection.execute(
                        "SELECT count(*),COALESCE(SUM(prompt_tokens),0),"
                        "COALESCE(SUM(total_tokens),0) FROM request_log "
                        "WHERE event_id LIKE 'mcode:%'").fetchone()
                return tuple(int(value) for value in row)

            with patch.dict(os.environ, {
                    "HOME": str(home), **self.MCODE_NO_LOCATORS}):
                # 旧布局是唯一的数据根:历史在这里导入。
                self.assertEqual(import_once(database), 2)
                self.assertEqual(imported_rows(), (2, 15, 17))

                # mcode 的迁移把整棵旧目录复制进默认根,副本的 rowid 与旧库
                # 不同(前面还有一行无效行):同一行不能改头换面再入库一次。
                self._write_mcode_db(
                    self._mcode_store_path(home / ".minimax"),
                    sessions=[("s1", "/work/primary", None)],
                    tokens=[
                        (None, "mcode-model", 1782720000000, 0, 0, 0, 0, 0),
                        ("s1", "mcode-model", 1782720000000, 10, 2, 0, 1, 0),
                        ("s1", "mcode-model", 1782720060000, 4, 0, 0, 0, 0),
                    ],
                )
                self.assertEqual(import_once(database), 0)
                self.assertEqual(imported_rows(), (2, 15, 17))

                # 默认根里的新用量照常入库。
                with sqlite_connection(
                        self._mcode_store_path(home / ".minimax")) as connection:
                    connection.execute(
                        "INSERT INTO local_runtime_token_usage "
                        "VALUES(?,?,?,?,?,?,?,?,NULL)",
                        ("s1", "mcode-model", 1782720120000, 3, 1, 0, 0, 0))
                self.assertEqual(import_once(database), 1)
                self.assertEqual(imported_rows(), (3, 18, 21))

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

    def test_grok_turn_completed_carries_native_api_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "sessions" / "work-project" / "grok-session"
            session.mkdir(parents=True)
            (session / "summary.json").write_text(
                json.dumps({"current_model_id": "grok-model"}),
                encoding="utf-8",
            )
            path = session / "updates.jsonl"
            path.write_text(
                json.dumps({
                    "timestamp": "2026-08-24T00:00:00Z",
                    "params": {"update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 100, "cachedReadTokens": 40,
                            "outputTokens": 20, "reasoningTokens": 5,
                            "modelUsage": {"grok-4.5-build": {
                                "inputTokens": 100, "cachedReadTokens": 40,
                                "outputTokens": 20, "apiDurationMs": 5342}},
                        },
                    }},
                }) + "\n" + json.dumps({
                    "timestamp": "2026-08-24T00:01:00Z",
                    "params": {"update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {"inputTokens": 10, "outputTokens": 2},
                    }},
                }) + "\n",
                encoding="utf-8",
            )

            parsed = grok.parse(UsageSource(
                path=path,
                context={"session_path": session, "session_id": "session-1"},
            ))
            self.assertEqual([event.duration_ms for event in parsed.events],
                             [5342, 0])

    def test_grok_usage_ledger_carries_native_api_duration(self) -> None:
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
                    "inputTokens": 10, "cachedReadTokens": 3,
                    "outputTokens": 4, "apiDurationMs": 1500,
                }],
            }), encoding="utf-8")

            items = grok.discover({"config": {"data_root": str(Path(directory))}})
            event = grok.parse(items[0]).events[0]
            self.assertEqual(event.duration_ms, 1500)

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

    def _write_hermes_store(self, home: Path, *, session_id: str, profile: str = "",
                            model: str = "hermes-model",
                            started_at: str = "2026-08-24T00:00:00Z",
                            input_tokens: int = 100, output_tokens: int = 20,
                            cache_read: int = 0, reasoning: int = 0,
                            cache_write: int = 0) -> Path:
        """写一个只含一条会话记录的 Hermes 库(home/state.db 或 home/profiles/<name>/state.db)。"""
        directory = home / "profiles" / profile if profile else home
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "state.db"
        fresh = not path.exists()
        with sqlite_connection(path) as connection:
            if fresh:
                connection.execute("""CREATE TABLE sessions (
                    id TEXT, model TEXT, started_at TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, cache_read_tokens INTEGER,
                    reasoning_tokens INTEGER, cache_write_tokens INTEGER
                )""")
            connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (session_id, model, started_at, input_tokens, output_tokens,
                 cache_read, reasoning, cache_write),
            )
        return path

    def _temporary_proxy_database(self, root: Path):
        """建立临时 ProxyDatabase(复制 schema 并升级),供导入端到端用例使用。"""
        import shutil

        from app.db.proxy_db import ProxyDatabase
        from app.db.schema_upgrade import ensure_local_databases

        shutil.copytree(PROJECT_ROOT / "schema", root / "schema")
        (root / "data").mkdir()
        proxy_path = root / "data" / "token-board.db"
        ensure_local_databases(str(proxy_path),
                              str(root / "data" / "dashboard.db"),
                              root / "schema")
        return ProxyDatabase(str(proxy_path)), proxy_path

    def test_hermes_extra_homes_are_additive_and_keep_the_richest_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            extra = root / "extra-home"
            self._write_hermes_store(home, session_id="shared")
            self._write_hermes_store(home, session_id="default-only",
                                     input_tokens=10, output_tokens=5)
            self._write_hermes_store(extra, profile="side", session_id="shared",
                                     input_tokens=500, output_tokens=90, reasoning=5)
            self._write_hermes_store(extra, session_id="extra-only",
                                     input_tokens=20, output_tokens=8)

            items = hermes.discover({"config": {
                "data_root": str(home),
                "extra_roots": {"hermes": [str(extra)]},
            }})
            self.assertEqual(len(items), 1)
            parsed = hermes.parse(items[0])
            self.assertFalse(parsed.skipped)
            by_session = {event.session_id: event for event in parsed.events}
            self.assertEqual(sorted(by_session),
                             ["default-only", "extra-only", "shared"])
            # 额外 home 只补充新会话;同一会话保留 token 更多的拷贝,两份不叠加。
            shared = by_session["shared"]
            self.assertEqual((shared.prompt_tokens, shared.completion_tokens,
                              shared.cache_read_tokens, shared.total_tokens,
                              shared.project), (500, 90, 0, 590, "side"))

    def test_hermes_copied_home_counts_a_shared_session_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            copy = root / "backup-copy"
            for target in (home, copy):
                self._write_hermes_store(target, session_id="copied-session")

            alone = hermes.parse(hermes.discover(
                {"config": {"data_root": str(home)}})[0])
            self.assertEqual(len(alone.events), 1)
            self.assertEqual(alone.events[0].total_tokens, 120)

            items = hermes.discover({"config": {
                "data_root": str(home),
                "extra_roots": {"hermes": [str(copy)]},
            }})
            parsed = hermes.parse(items[0])
            self.assertFalse(parsed.skipped)
            # 路径身份只能防同一文件读两遍;内容拷贝由会话身份去重,并列时更早的
            # 默认 home 胜出,且事件身份不随库变化,重复导入仍然幂等。
            self.assertEqual(len(parsed.events), 1)
            event = parsed.events[0]
            self.assertEqual((event.total_tokens, event.project), (120, "default"))
            self.assertEqual([item.event_id for item in alone.events],
                             [event.event_id])

    def test_hermes_earlier_extra_root_wins_a_token_tie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            first = root / "first-extra"
            second = root / "second-extra"
            self._write_hermes_store(home, session_id="home-only",
                                     input_tokens=1, output_tokens=2)
            self._write_hermes_store(first, profile="alpha", session_id="shared")
            self._write_hermes_store(second, profile="beta", session_id="shared")

            items = hermes.discover({"config": {
                "data_root": str(home),
                "extra_roots": {"hermes": [str(first), str(second)]},
            }})
            parsed = hermes.parse(items[0])
            self.assertFalse(parsed.skipped)
            shared = [event for event in parsed.events
                      if event.session_id == "shared"]
            self.assertEqual(len(shared), 1)
            self.assertEqual(shared[0].project, "alpha")

    def test_hermes_symlinked_extra_home_is_deduplicated_by_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            self._write_hermes_store(home, session_id="linked-session")
            link = root / "linked-home"
            link.symlink_to(home, target_is_directory=True)

            items = hermes.discover({"config": {
                "data_root": str(home),
                "extra_roots": {"hermes": [str(link)]},
            }})
            parsed = hermes.parse(items[0])
            self.assertFalse(parsed.skipped)
            self.assertEqual(len(parsed.events), 1)
            self.assertEqual(parsed.events[0].total_tokens, 120)

    def test_hermes_missing_extra_home_skips_instead_of_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            self._write_hermes_store(home, session_id="hidden-session")
            missing = root / "missing-hermes"

            items = hermes.discover({"config": {
                "data_root": str(home),
                "extra_roots": {"hermes": [str(missing)]},
            }})
            self.assertEqual(len(items), 1)
            parsed = hermes.parse(items[0])
            # 无效额外根不是空成功:整批 skipped,导入器因此保留该来源上次的游标。
            self.assertTrue(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertIn("额外根目录不可用", parsed.warnings[0])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root 不受 chmod 限制,无法构造不可读目录")
    def test_hermes_unreadable_extra_home_skips_the_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            self._write_hermes_store(home, session_id="hidden-session")
            extra = root / "locked-hermes"
            self._write_hermes_store(extra, session_id="locked-session")
            os.chmod(extra, 0)
            try:
                items = hermes.discover({"config": {
                    "data_root": str(home),
                    "extra_roots": {"hermes": [str(extra)]},
                }})
                parsed = hermes.parse(items[0])
            finally:
                os.chmod(extra, 0o700)
            self.assertTrue(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertIn("额外根目录读取失败", parsed.warnings[0])

    def test_hermes_import_keeps_a_shared_session_at_one_request_log_row(self) -> None:
        from app.services.agent_usage.importer import import_once

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            extra = root / "extra-home"
            self._write_hermes_store(home, session_id="shared-session")
            self._write_hermes_store(home, session_id="home-only",
                                     input_tokens=10, output_tokens=5)
            self._write_hermes_store(extra, session_id="extra-only",
                                     input_tokens=20, output_tokens=8)
            database, proxy_path = self._temporary_proxy_database(root)
            software_id = database.create_agent_software({
                "name": "hermes-agent", "agent_kind": "hermes",
                "config": {"data_root": str(home),
                           "extra_roots": {"hermes": [str(extra)]}},
            })
            self.assertEqual(import_once(database), 3)

            # 额外 home 随后出现同一会话的完整拷贝:胜出的库变了,事件身份不变,
            # request_log 不会为同一历史再加一行。
            self._write_hermes_store(extra, session_id="shared-session",
                                     input_tokens=500, output_tokens=90, reasoning=5)
            self.assertEqual(import_once(database), 0)
            with sqlite_connection(proxy_path) as connection:
                rows = connection.execute(
                    "SELECT event_id FROM request_log WHERE agent_software_id=? "
                    "AND event_id LIKE 'hermes:%shared-session%'", (software_id,),
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM request_log WHERE agent_software_id=?",
                    (software_id,)).fetchone()[0], 3)

            # 再次导入幂等:回执按事件身份去重。
            self.assertEqual(import_once(database), 0)
            with sqlite_connection(proxy_path) as connection:
                before = connection.execute(
                    "SELECT cursor_json FROM agent_software_runtime "
                    "WHERE software_id=?", (software_id,)).fetchone()[0]

            # 额外根随后缺失:整批 skipped,游标停留在上次同步状态。
            extra.rename(root / "extra-home-moved")
            self.assertEqual(import_once(database), 0)
            with sqlite_connection(proxy_path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM request_log WHERE agent_software_id=?",
                    (software_id,)).fetchone()[0], 3)
                row = connection.execute(
                    "SELECT cursor_json,last_error FROM agent_software_runtime "
                    "WHERE software_id=?", (software_id,)).fetchone()
            self.assertEqual(row[0], before)
            self.assertIn("额外根目录不可用", row[1])

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

    def test_opencode_reads_v2_session_message_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            with sqlite_connection(root / "opencode.db") as connection:
                connection.execute(
                    "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT)")
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute(
                    "INSERT INTO session_v2 VALUES(?,?)",
                    ("ses_v2", "/work/v2-project"))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_u1", "ses_v2", "user", 1782720000000,
                     json.dumps({"type": "user",
                                 "time": {"created": 1782720000000}})))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_a1", "ses_v2", "assistant", 1782720001000,
                     json.dumps({
                         "type": "assistant",
                         "time": {"created": 1782720001000},
                         "model": {"id": "claude-opus-4-6",
                                   "providerID": "opencode"},
                         "tokens": {"input": 10, "output": 3, "reasoning": 1,
                                    "cache": {"read": 2, "write": 4}},
                     })))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_idle", "ses_v2", "idle", 1782720002000,
                     json.dumps({"type": "idle"})))

            items = opencode.discover({"config": {"data_root": str(root)}})
            self.assertEqual(len(items), 1)
            parsed = opencode.parse(items[0])
            # role 取 type、model 取 data.model.id、created 取 data.time.created、
            # project 取 session_v2.directory;user/idle 行不计费。
            self.assert_one_event(
                parsed, model="claude-opus-4-6", prompt=14, completion=4,
                cache=2, total=18, project="v2-project", session_id="ses_v2")
            self.assertEqual(
                parsed.events[0].requested_at, "2026-06-29T08:00:01Z")

    def test_opencode_v2_falls_back_to_session_table_and_message_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            with sqlite_connection(root / "opencode.db") as connection:
                connection.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute("INSERT INTO session VALUES(?,?)",
                                   ("ses_plain", "/work/plain-session"))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_a1", "ses_plain", "assistant", 1782720000000,
                     json.dumps({
                         "type": "assistant",
                         "modelID": "kimi-k2.5",
                         "tokens": {"input": 5, "output": 2},
                     })))

            items = opencode.discover({"config": {"data_root": str(root)}})
            events = opencode.parse(items[0]).events
            # 没有 session_v2 时回退到 pre-split 的 session 表;data.time.created
            # 缺失时回退 time_created 列;data.model.id 缺失时回退 modelID。
            self.assertEqual(len(events), 1)
            self.assertEqual(
                (events[0].model, events[0].prompt_tokens,
                 events[0].completion_tokens, events[0].project,
                 events[0].requested_at),
                ("kimi-k2.5", 5, 2, "plain-session", "2026-06-29T08:00:00Z"))

    def test_opencode_merges_v2_copy_behind_legacy_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            payload = {
                "role": "assistant", "time": {"created": 1782720000000},
                "modelID": "opencode-model",
                "tokens": {"input": 10, "output": 2, "cache": {"read": 1}},
                "path": {"root": "/work/project"},
            }
            with sqlite_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, data TEXT)")
                connection.execute(
                    "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT)")
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute("INSERT INTO message VALUES(?,?,?)",
                                   ("message-1", "session-1", json.dumps(payload)))
                connection.execute("INSERT INTO session_v2 VALUES(?,?)",
                                   ("session-1", "/work/migrated-project"))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("message-1", "session-1", "assistant", 1782720000000,
                     json.dumps({key: value for key, value in payload.items()
                                 if key != "path"})))

            items = opencode.discover({"config": {"data_root": str(path)}})
            parsed = opencode.parse(items[0])
            # 两代存储共存:按 (session, message id) 只计一次,legacy 行先出,
            # 因此已上传过的 project 名不会被 2.x 的 directory 改写。
            self.assert_one_event(
                parsed, model="opencode-model", prompt=10, completion=2,
                cache=1, total=12, project="project", session_id="session-1")

    def test_opencode_unknown_store_schema_warns_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            with sqlite_connection(root / "opencode.db") as connection:
                connection.execute("CREATE TABLE unrelated (id TEXT)")

            items = opencode.discover({"config": {"data_root": str(root)}})
            parsed = opencode.parse(items[0])
            self.assertTrue(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertTrue(any(
                "不认识的表结构" in warning and "session_message" in warning
                for warning in parsed.warnings), parsed.warnings)

    def test_opencode_probe_ignores_table_name_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            with sqlite_connection(path) as connection:
                # SQLite 标识符不区分大小写,表名探测不应把大小写变体
                # 误判成不认识的表结构。
                connection.execute(
                    'CREATE TABLE "Message" (id TEXT, session_id TEXT, data TEXT)')
                connection.execute(
                    'INSERT INTO "Message"(id,data,session_id) VALUES(?,?,?)',
                    ("message-1", json.dumps({
                        "role": "assistant", "time": {"created": 1782720000000},
                        "modelID": "opencode-model",
                        "tokens": {"input": 5, "output": 1},
                        "path": {"root": "/work/project"},
                    }), "session-1"))

            items = opencode.discover({"config": {"data_root": str(path)}})
            self.assert_one_event(
                opencode.parse(items[0]), model="opencode-model", prompt=5,
                completion=1, cache=0, total=6, project="project",
                session_id="session-1")

    def test_opencode_missing_store_file_is_an_empty_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # discover 与 parse 之间文件消失时保持原有行为:空结果而非告警,
            # 由导入器的 stat 检查负责跳过。
            parsed = opencode.parse(UsageSource(
                path=Path(directory) / "opencode.db",
                context={"format": "sqlite"},
            ))
            self.assertFalse(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertEqual(parsed.warnings, ())

    def test_opencode_v2_without_session_table_reads_unknown_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "opencode"
            root.mkdir()
            with sqlite_connection(root / "opencode.db") as connection:
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_a1", "ses_x", "assistant", 1782720000000,
                     json.dumps({
                         "type": "assistant",
                         "model": {"id": "opencode-model"},
                         "tokens": {"input": 5, "output": 1},
                     })))

            items = opencode.discover({"config": {"data_root": str(root)}})
            # 没有 session / session_v2 表时仍读计价数据,project 回落 unknown。
            self.assert_one_event(
                opencode.parse(items[0]), model="opencode-model", prompt=5,
                completion=1, cache=0, total=6, project="unknown",
                session_id="ses_x")

    def test_opencode_folds_cache_write_into_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            with sqlite_connection(path) as connection:
                connection.execute(
                    "CREATE TABLE message(id TEXT, data TEXT, session_id TEXT)")
                connection.execute(
                    "INSERT INTO message VALUES(?,?,?)",
                    ("message-1", json.dumps({
                        "role": "assistant", "time": {"created": 1782720000000},
                        "modelID": "opencode-model",
                        "tokens": {"input": 10, "output": 2,
                                    "cache": {"read": 5, "write": 7}},
                        "path": {"root": "/work/project"},
                    }), "session-1"))

            items = opencode.discover({"config": {"data_root": str(path)}})
            # tokens.cache.write 折进 input(IR 没有单独的写缓存列)。
            self.assert_one_event(
                opencode.parse(items[0]), model="opencode-model", prompt=17,
                completion=2, cache=5, total=19, project="project",
                session_id="session-1")

    def test_opencode_windows_localappdata_root_is_additive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xdg = root / "xdg" / "opencode"
            native = root / "localappdata" / "opencode"
            for path in (xdg, native):
                path.mkdir(parents=True)
                (path / "opencode.db").write_bytes(b"")

            def store_roots() -> set[Path]:
                return {Path(store["path"]).resolve().parent
                        for store in opencode.discover({})[0].context["stores"]}

            with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "localappdata")}):
                os.environ.pop("VIBE_USAGE_OPENCODE_DIRS", None)
                os.environ.pop("VIBE_USAGE_OPENCODE_EXTRA_ROOTS", None)
                os.environ.pop("OPENCODE_EXTRA_ROOTS", None)
                with patch.object(opencode, "DEFAULT_PATH", xdg):
                    # 存在时追加原生根,且不替代 XDG。
                    with patch("sys.platform", "win32"):
                        self.assertEqual(
                            store_roots(), {xdg.resolve(), native.resolve()})
                    # 非 Windows 平台不探测 LOCALAPPDATA。
                    with patch("sys.platform", "linux"):
                        self.assertEqual(store_roots(), {xdg.resolve()})
                    # 目录不存在时不追加。
                    (native / "opencode.db").unlink()
                    native.rmdir()
                    with patch("sys.platform", "win32"):
                        self.assertEqual(store_roots(), {xdg.resolve()})

    def _import_opencode_store(self, root: Path, store: Path) -> tuple[int, Path]:
        """经真实导入路径导出一个临时 opencode 存储,返回 (插入行数, 代理库路径)。"""
        import shutil

        from app.db.proxy_db import ProxyDatabase
        from app.db.schema_upgrade import ensure_local_databases
        from app.services.agent_usage.importer import import_once
        from app.tests.support import PROJECT_ROOT

        shutil.copytree(PROJECT_ROOT / "schema", root / "schema")
        (root / "data").mkdir()
        proxy_path = root / "data" / "token-board.db"
        ensure_local_databases(
            str(proxy_path), str(root / "data" / "dashboard.db"),
            root / "schema")
        database = ProxyDatabase(str(proxy_path))
        database.create_agent_software({
            "name": "opencode-agent", "agent_kind": "opencode",
            "config": {"data_root": str(store)},
        })
        return import_once(database), proxy_path

    def test_opencode_v2_store_imports_through_import_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "opencode"
            store.mkdir()
            with sqlite_connection(store / "opencode.db") as connection:
                connection.execute(
                    "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT)")
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute("INSERT INTO session_v2 VALUES(?,?)",
                                   ("ses_v2", "/work/imported-project"))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    ("msg_a1", "ses_v2", "assistant", 1782720000000,
                     json.dumps({
                         "type": "assistant",
                         "time": {"created": 1782720000000},
                         "model": {"id": "claude-opus-4-6"},
                         "tokens": {"input": 10, "output": 3, "reasoning": 1,
                                    "cache": {"read": 2, "write": 4}},
                     })))

            inserted, proxy_path = self._import_opencode_store(root, store)
            self.assertEqual(inserted, 1)
            with sqlite_connection(proxy_path) as connection:
                row = connection.execute(
                    "SELECT model,prompt_tokens,completion_tokens,"
                    "cache_read_tokens,total_tokens,requested_at "
                    "FROM request_log WHERE event_id LIKE 'opencode:%'"
                ).fetchone()
            self.assertEqual(tuple(row), (
                "claude-opus-4-6", 14, 4, 2, 18, "2026-06-29T08:00:00Z"))

    def test_opencode_idless_rows_keep_distinct_event_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "opencode"
            store.mkdir()
            payload = {
                "role": "assistant", "time": {"created": 1782720000000},
                "modelID": "opencode-model",
                "tokens": {"input": 10, "output": 2},
            }
            with sqlite_connection(store / "opencode.db") as connection:
                # 两代表示各一行,均没有消息 id(旧表无 id 列,投影行 id 为 NULL),
                # 且 session 与 rowid 相同:序号必须带上来源因子才不会撞车。
                connection.execute("CREATE TABLE message (data TEXT, session_id TEXT)")
                connection.execute(
                    "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT)")
                connection.execute(
                    "CREATE TABLE session_message (id TEXT PRIMARY KEY, "
                    "session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
                connection.execute("INSERT INTO message VALUES(?,?)",
                                   (json.dumps(payload), "session-1"))
                connection.execute("INSERT INTO session_v2 VALUES(?,?)",
                                   ("session-1", "/work/project"))
                connection.execute(
                    "INSERT INTO session_message VALUES(?,?,?,?,?)",
                    (None, "session-1", "assistant", 1782720000000,
                     json.dumps(payload)))

            items = opencode.discover({"config": {"data_root": str(store)}})
            parsed = opencode.parse(items[0])
            self.assertEqual(len(parsed.events), 2)
            self.assertEqual(len({event.event_id for event in parsed.events}), 2)

            inserted, proxy_path = self._import_opencode_store(root, store)
            self.assertEqual(inserted, 2)
            with sqlite_connection(proxy_path) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM request_log WHERE event_id LIKE 'opencode:%'"
                ).fetchone()[0]
            self.assertEqual(count, 2)

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

    # --- Codex durable 用量记录与 RequestTimer(移植 ref/vibe-usage、ref/cc-switch) ---

    @staticmethod
    def _codex_usage(input_tokens: int, cached: int, output: int,
                     reasoning: int) -> dict:
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "output_tokens": output, "reasoning_output_tokens": reasoning,
                "total_tokens": input_tokens + output}

    @classmethod
    def _codex_durable(cls, at: str, thread_total=None, request_usage=None) -> dict:
        """durable `token_usage_record`:请求级用量 + 线程累计用量。"""
        return {
            "timestamp": at,
            "type": "token_usage_record",
            "payload": {
                "usage": request_usage if request_usage is not None
                else cls._codex_usage(100, 20, 10, 2),
                "thread_token_usage": thread_total if thread_total is not None
                else cls._codex_usage(100, 20, 10, 2),
            },
        }

    @classmethod
    def _codex_ui_token_count(cls, at: str, thread_total=None,
                              request_usage=None) -> dict:
        return {
            "timestamp": at,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {
                "last_token_usage": request_usage if request_usage is not None
                else cls._codex_usage(100, 20, 10, 2),
                "total_token_usage": thread_total if thread_total is not None
                else cls._codex_usage(100, 20, 10, 2),
            }},
        }

    @staticmethod
    def _codex_session_meta() -> dict:
        return {"timestamp": "2026-09-20T00:00:00.000Z", "type": "session_meta",
                "payload": {"id": "durable-session"}}

    @staticmethod
    def _codex_turn_context(model: str = "gpt-durable") -> dict:
        return {"timestamp": "2026-09-20T00:00:00.000Z", "type": "turn_context",
                "payload": {"model": model}}

    @staticmethod
    def _codex_response_item(at: str, item_type: str,
                             role: str | None = None) -> dict:
        payload = {"type": item_type}
        if role is not None:
            payload["role"] = role
        return {"timestamp": at, "type": "response_item", "payload": payload}

    @staticmethod
    def _codex_token_count(at: str, total: tuple[int, int],
                           last: tuple[int, int]) -> dict:
        def usage(pair: tuple[int, int]) -> dict:
            return {"input_tokens": pair[0], "cached_input_tokens": 0,
                    "output_tokens": pair[1], "reasoning_output_tokens": 0,
                    "total_tokens": pair[0] + pair[1]}

        return {"timestamp": at, "type": "event_msg", "payload": {
            "type": "token_count",
            "info": {"total_token_usage": usage(total),
                     "last_token_usage": usage(last)},
        }}

    @staticmethod
    def _codex_raw_usage_record(at: str, tokens: tuple[int, int]) -> dict:
        """只有请求级 usage、没有线程累计的 `token_usage_record`(不可归一)。"""
        return {"timestamp": at, "type": "token_usage_record", "payload": {
            "usage": {"input_tokens": tokens[0], "cached_input_tokens": 0,
                      "output_tokens": tokens[1],
                      "reasoning_output_tokens": 0,
                      "total_tokens": tokens[0] + tokens[1]},
        }}

    def _parse_codex_records(self, lines) -> object:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions" / "2026" / "09" / "20"
            root.mkdir(parents=True)
            path = root / "rollout-durable.jsonl"
            path.write_text("\n".join(json.dumps(value) for value in lines) + "\n",
                            encoding="utf-8")
            item = codex.discover({"config": {"data_root": directory}})[0]
            return codex.parse(item)

    def _codex_durations(self, lines) -> list[int]:
        return [event.duration_ms
                for event in self._parse_codex_records(lines).events]

    def test_codex_durable_usage_record_without_ui_event_is_counted(self) -> None:
        parsed = self._parse_codex_records([
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_durable("2026-09-20T00:00:01.000Z"),
        ])
        self.assertFalse(parsed.skipped)
        self.assertEqual(len(parsed.events), 1)
        event = parsed.events[0]
        self.assertEqual(
            (event.model, event.prompt_tokens, event.completion_tokens,
             event.cache_read_tokens, event.total_tokens),
            ("gpt-durable", 100, 10, 20, 110),
        )
        self.assertEqual(event.session_id, "durable-session")

    def test_codex_ui_mirror_is_paired_with_the_durable_record(self) -> None:
        # UI 事件的累计计数落后于 durable 记录:配对只能看请求级用量。
        parsed = self._parse_codex_records([
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_durable("2026-09-20T00:00:01.000Z"),
            self._codex_ui_token_count("2026-09-20T00:00:02.000Z",
                                       self._codex_usage(90, 20, 10, 2)),
            self._codex_durable("2026-09-20T00:00:03.000Z",
                                self._codex_usage(200, 40, 20, 4)),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z",
                                       self._codex_usage(180, 40, 20, 4)),
        ])
        self.assertEqual(len(parsed.events), 2)
        self.assertEqual(sum(event.prompt_tokens for event in parsed.events), 200)
        self.assertEqual(sum(event.completion_tokens for event in parsed.events), 20)

    def test_codex_dual_baselines_keep_the_interrupted_series_apart(self) -> None:
        # durable 与 UI 两条累计线各自判重:UI 系列在中断后分叉,不能拿
        # durable 的累计值当基线,否则会漏计或重复计。
        parsed = self._parse_codex_records([
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_durable("2026-09-20T00:00:01.000Z"),
            self._codex_ui_token_count("2026-09-20T00:00:02.000Z",
                                       self._codex_usage(90, 20, 10, 2)),
            # durable 累计未变 → 重复快照;UI 累计未变 → 同样重复
            self._codex_durable("2026-09-20T00:00:03.000Z"),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z",
                                       self._codex_usage(90, 20, 10, 2)),
        ])
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].prompt_tokens, 100)

    def test_codex_missing_ui_mirror_does_not_suppress_the_next_request(self) -> None:
        parsed = self._parse_codex_records([
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_durable("2026-09-20T00:00:01.000Z"),
            self._codex_durable("2026-09-20T00:00:03.000Z",
                                self._codex_usage(200, 40, 20, 4)),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z"),
        ])
        self.assertEqual(len(parsed.events), 2)
        self.assertEqual(sum(event.prompt_tokens for event in parsed.events), 200)

    def test_codex_malformed_durable_records_are_rejected(self) -> None:
        negative = self._codex_usage(-1, 0, 0, 0)
        not_a_number = self._codex_usage(100, 20, 10, 2)
        not_a_number["input_tokens"] = float("nan")
        parsed = self._parse_codex_records([
            self._codex_session_meta(),
            self._codex_turn_context(),
            # 负值、NaN、累计用量非正:都原样忽略,不能顶掉后面的真实用量
            self._codex_durable("2026-09-20T00:00:01.000Z",
                                self._codex_usage(100, 20, 10, 2), negative),
            self._codex_durable("2026-09-20T00:00:02.000Z",
                                self._codex_usage(100, 20, 10, 2), not_a_number),
            self._codex_durable("2026-09-20T00:00:03.000Z",
                                self._codex_usage(0, 0, 0, 0)),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z"),
        ])
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].prompt_tokens, 100)
        self.assertEqual(parsed.events[0].completion_tokens, 10)

    def test_codex_segment_merge_keeps_durable_records_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_id = "durable-segment-session"
            live = Path(directory) / "sessions" / "2026" / "09" / "20"
            archived = Path(directory) / "archived_sessions"
            live.mkdir(parents=True)
            archived.mkdir()
            first = [self._codex_session_meta(),
                     self._codex_turn_context("gpt-segment"),
                     self._codex_durable("2026-09-20T00:00:01.000Z")]
            # 第二段是同一次会话的续写,开头重放了第一段的记录
            second = [self._codex_session_meta(),
                      self._codex_turn_context("gpt-segment"),
                      self._codex_durable("2026-09-20T00:00:01.000Z"),
                      self._codex_ui_token_count("2026-09-20T00:00:02.000Z",
                                                 self._codex_usage(90, 20, 10, 2)),
                      self._codex_durable("2026-09-20T00:00:03.000Z",
                                          self._codex_usage(200, 40, 20, 4))]
            name = f"rollout-20260920000000-{session_id}.jsonl"
            (live / name).write_text(
                "\n".join(json.dumps(value) for value in first) + "\n",
                encoding="utf-8")
            (archived / name).write_text(
                "\n".join(json.dumps(value) for value in second) + "\n",
                encoding="utf-8")
            item = codex.discover({"config": {"data_root": directory}})[0]
            self.assertEqual(len(item.context["segments"]), 2)
            parsed = codex.parse(item)
        # 重放的 durable 记录按指纹去重一次,UI mirror 不双计;模型取自所属段
        self.assertEqual(len(parsed.events), 2)
        self.assertEqual(sum(event.prompt_tokens for event in parsed.events), 200)
        self.assertEqual({event.model for event in parsed.events}, {"gpt-segment"})

    def test_codex_segment_merge_timing_keeps_the_durable_end_time(self) -> None:
        # 多段合并后 durable 记录已是 UI 形状,但它仍是一次响应结束时的用量
        # 记录:同批落盘的工具结果不改变终点(01.000 → 09.500)。
        with tempfile.TemporaryDirectory() as directory:
            session_id = "durable-timing-session"
            live = Path(directory) / "sessions" / "2026" / "09" / "20"
            archived = Path(directory) / "archived_sessions"
            live.mkdir(parents=True)
            archived.mkdir()
            opening = [self._codex_session_meta(),
                       self._codex_response_item(
                           "2026-10-02T03:00:01.000Z", "message", "user"),
                       self._codex_response_item(
                           "2026-10-02T03:00:06.000Z", "custom_tool_call")]
            continuation = opening + [
                # 工具结果与 durable 记录同一毫秒落盘,工具结果写在前面
                self._codex_response_item(
                    "2026-10-02T03:00:09.500Z", "custom_tool_call_output"),
                self._codex_durable(
                    "2026-10-02T03:00:09.500Z",
                    request_usage=self._codex_usage(100, 0, 600, 0)),
                self._codex_ui_token_count(
                    "2026-10-02T03:00:20.001Z",
                    thread_total=self._codex_usage(100, 0, 600, 0),
                    request_usage=self._codex_usage(100, 0, 600, 0)),
            ]
            name = f"rollout-20261002000000-{session_id}.jsonl"
            (live / name).write_text(
                "\n".join(json.dumps(value) for value in opening) + "\n",
                encoding="utf-8")
            (archived / name).write_text(
                "\n".join(json.dumps(value) for value in continuation) + "\n",
                encoding="utf-8")
            item = codex.discover({"config": {"data_root": directory}})[0]
            self.assertEqual(len(item.context["segments"]), 2)
            parsed = codex.parse(item)
        # UI mirror 不双计,耗时止于 durable 记录的时刻
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].duration_ms, 8_500)

    def test_codex_fork_replay_of_durable_records_counts_only_new_usage(self) -> None:
        # 子会话重放父会话的 UI token_count 与 durable 记录:两条线都要按指纹
        # 跳过,只有尾部真实新增的那次请求计数。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions" / "2026" / "10" / "02"
            root.mkdir(parents=True)
            parent_records = [
                self._codex_durable("2026-10-02T01:01:00Z",
                                    self._codex_usage(10, 0, 1, 0),
                                    self._codex_usage(10, 0, 1, 0)),
                self._codex_ui_token_count("2026-10-02T01:01:01Z",
                                           self._codex_usage(10, 0, 1, 0),
                                           self._codex_usage(10, 0, 1, 0)),
                self._codex_durable("2026-10-02T01:02:00Z",
                                    self._codex_usage(30, 0, 3, 0),
                                    self._codex_usage(20, 0, 2, 0)),
                self._codex_ui_token_count("2026-10-02T01:02:01Z",
                                           self._codex_usage(30, 0, 3, 0),
                                           self._codex_usage(20, 0, 2, 0)),
            ]
            parent = [{"timestamp": "2026-10-02T01:00:00Z",
                       "type": "session_meta",
                       "payload": {"id": "replay-parent"}}] + parent_records
            replayed = [
                dict(record, timestamp="2026-10-02T02:00:00Z")
                for record in parent_records
            ]
            child = [{"timestamp": "2026-10-02T02:00:00Z", "type": "session_meta",
                      "payload": {"id": "replay-child",
                                  "forked_from_id": "replay-parent"}}]
            child += replayed
            child.append(self._codex_ui_token_count(
                "2026-10-02T02:00:05Z", self._codex_usage(35, 0, 6, 0),
                self._codex_usage(5, 0, 3, 0)))
            (root / "parent.jsonl").write_text(
                "\n".join(json.dumps(value) for value in parent) + "\n",
                encoding="utf-8")
            (root / "fork.jsonl").write_text(
                "\n".join(json.dumps(value) for value in child) + "\n",
                encoding="utf-8")

            sources = codex.discover({"config": {"data_root": directory}})
            skips = codex.replay_skips(sources)
            parsed = {}
            for item in sources:
                parsed[str(item.context["session_id"])] = codex.parse(
                    item, skip_token_count=skips.get(str(item.path), 0))
        self.assertEqual(skips[str(
            Path(directory) / "sessions" / "2026" / "10" / "02" / "fork.jsonl")],
            len(parent_records))
        self.assertEqual(
            [(event.prompt_tokens, event.completion_tokens)
             for event in parsed["replay-parent"].events],
            [(10, 1), (20, 2)])
        self.assertEqual(
            [(event.prompt_tokens, event.completion_tokens)
             for event in parsed["replay-child"].events],
            [(5, 3)])

    @staticmethod
    def _codex_ui_ids_for_lines(lines) -> list[str]:
        """旧实现下单文件 UI token_count 事件的 event_id(物理行号)。"""
        return [f"codex:session:durable-session:{index}"
                for index, value in enumerate(lines, 1)
                if value.get("type") == "event_msg"
                and value["payload"].get("type") == "token_count"]

    def test_codex_ui_event_ids_keep_legacy_line_identity(self) -> None:
        lines = [
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_response_item("2026-09-20T00:00:01.000Z", "message", "user"),
            # durable 记录与其 UI mirror:由 UI 事件代表这次请求,沿用旧 id
            self._codex_durable("2026-09-20T00:00:02.000Z"),
            self._codex_ui_token_count("2026-09-20T00:00:02.500Z"),
            self._codex_response_item("2026-09-20T00:00:03.000Z", "reasoning"),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z",
                                       self._codex_usage(200, 20, 10, 2),
                                       self._codex_usage(100, 20, 10, 2)),
            # 中断的请求只留下 durable 记录:用不与整数序号碰撞的 record:N
            self._codex_durable("2026-09-20T00:00:05.000Z",
                                self._codex_usage(200, 20, 10, 2),
                                self._codex_usage(60, 0, 30, 0)),
            self._codex_response_item("2026-09-20T00:00:06.000Z", "message",
                                      "assistant"),
        ]
        ids = [event.event_id for event in self._parse_codex_records(lines).events]
        self.assertEqual(ids[:2], self._codex_ui_ids_for_lines(lines))
        self.assertEqual(ids[2], "codex:session:durable-session:record:1")
        self.assertNotIn(ids[2], ids[:2])

    def test_codex_ui_event_ids_are_unchanged_by_durable_records(self) -> None:
        # 同一批 UI 记录(位置相同)在无 durable 与 durable 交错两种布局下,
        # event_id 必须完全一致,否则升级会重导在库历史。
        filler = {"timestamp": "2026-09-20T00:00:01.500Z", "type": "response_item",
                  "payload": {"type": "reasoning"}}
        legacy = [
            self._codex_session_meta(),
            self._codex_turn_context(),
            filler,
            self._codex_ui_token_count("2026-09-20T00:00:02.500Z"),
            filler,
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z",
                                       self._codex_usage(200, 20, 10, 2)),
        ]
        interleaved = [
            self._codex_session_meta(),
            self._codex_turn_context(),
            self._codex_durable("2026-09-20T00:00:01.500Z"),
            self._codex_ui_token_count("2026-09-20T00:00:02.500Z"),
            self._codex_durable("2026-09-20T00:00:03.500Z",
                                self._codex_usage(200, 20, 10, 2)),
            self._codex_ui_token_count("2026-09-20T00:00:04.000Z",
                                       self._codex_usage(200, 20, 10, 2)),
        ]
        legacy_events = self._parse_codex_records(legacy).events
        interleaved_events = self._parse_codex_records(interleaved).events
        self.assertEqual(
            [event.event_id for event in interleaved_events],
            [event.event_id for event in legacy_events])
        self.assertEqual([event.event_id for event in legacy_events],
                         self._codex_ui_ids_for_lines(legacy))
        self.assertEqual(
            [(event.prompt_tokens, event.completion_tokens)
             for event in interleaved_events],
            [(event.prompt_tokens, event.completion_tokens)
             for event in legacy_events])

    def test_codex_merged_ui_event_ids_ignore_durable_records(self) -> None:
        # 合并来源沿用旧实现的 UI 记录序号:durable 记录既不占号也不改号。
        with tempfile.TemporaryDirectory() as directory:
            session_id = "merged-id-session"
            live = Path(directory) / "sessions" / "2026" / "09" / "20"
            archived = Path(directory) / "archived_sessions"
            live.mkdir(parents=True)
            archived.mkdir()
            opening = [self._codex_session_meta(),
                       self._codex_turn_context(),
                       self._codex_durable("2026-09-20T00:00:01.000Z"),
                       self._codex_ui_token_count("2026-09-20T00:00:01.500Z")]
            continuation = opening + [
                self._codex_durable("2026-09-20T00:00:02.000Z",
                                    self._codex_usage(200, 20, 10, 2)),
                self._codex_ui_token_count("2026-09-20T00:00:02.500Z",
                                           self._codex_usage(200, 20, 10, 2)),
                self._codex_durable("2026-09-20T00:00:03.000Z",
                                    self._codex_usage(300, 20, 10, 2),
                                    self._codex_usage(60, 0, 30, 0)),
            ]
            name = f"rollout-20260920000000-{session_id}.jsonl"
            (live / name).write_text(
                "\n".join(json.dumps(value) for value in opening) + "\n",
                encoding="utf-8")
            (archived / name).write_text(
                "\n".join(json.dumps(value) for value in continuation) + "\n",
                encoding="utf-8")
            item = codex.discover({"config": {"data_root": directory}})[0]
            self.assertEqual(len(item.context["segments"]), 2)
            events = codex.parse(item).events
        self.assertEqual(
            [event.event_id for event in events],
            ["codex:session:durable-session:1",
             "codex:session:durable-session:2",
             "codex:session:durable-session:record:1"])

    def test_codex_durable_record_settles_at_its_own_timestamp(self) -> None:
        self.assertEqual(self._codex_durations([
            self._codex_session_meta(),
            self._codex_response_item("2026-10-02T03:00:01.000Z", "message", "user"),
            self._codex_response_item("2026-10-02T03:00:05.000Z", "reasoning"),
            self._codex_durable("2026-10-02T03:00:09.500Z",
                                request_usage=self._codex_usage(100, 0, 600, 0)),
        ]), [8_500])

    def test_codex_latency_stops_before_tool_execution_in_the_same_flush(self) -> None:
        # token_count 紧跟着工具结果写出(同一批):止于最后一个输出项的时刻,
        # 工具执行时间不算生成时间;重复快照不计时。
        self.assertEqual(self._codex_durations([
            self._codex_response_item("2026-08-03T03:00:01.000Z", "message", "user"),
            self._codex_response_item("2026-08-03T03:00:06.000Z", "custom_tool_call"),
            self._codex_response_item("2026-08-03T03:00:16.000Z",
                                      "custom_tool_call_output"),
            self._codex_token_count("2026-08-03T03:00:16.001Z", (100, 600),
                                    (100, 600)),
            self._codex_token_count("2026-08-03T03:00:17.000Z", (100, 600),
                                    (100, 600)),
            self._codex_response_item("2026-08-03T03:00:20.000Z", "reasoning"),
            self._codex_response_item("2026-08-03T03:00:22.000Z", "message",
                                      "assistant"),
            self._codex_token_count("2026-08-03T03:00:22.100Z", (300, 1300),
                                    (200, 700)),
        ]), [5_000, 6_099])

    def test_codex_latency_prefers_the_usage_record_end_time(self) -> None:
        self.assertEqual(self._codex_durations([
            self._codex_response_item("2026-10-02T03:00:01.000Z", "message", "user"),
            self._codex_response_item("2026-10-02T03:00:05.000Z", "reasoning"),
            self._codex_response_item("2026-10-02T03:00:09.000Z", "custom_tool_call"),
            # 响应在这里结束;token_count 要等工具跑完才写
            self._codex_raw_usage_record("2026-10-02T03:00:09.500Z", (100, 600)),
            self._codex_response_item("2026-10-02T03:00:20.000Z",
                                      "custom_tool_call_output"),
            self._codex_token_count("2026-10-02T03:00:20.001Z", (100, 600),
                                    (100, 600)),
            # 第二次请求:用量记录对不上,不采信,退回 token_count 的时刻
            self._codex_response_item("2026-10-02T03:00:25.000Z", "reasoning"),
            self._codex_response_item("2026-10-02T03:00:30.000Z", "message",
                                      "assistant"),
            self._codex_raw_usage_record("2026-10-02T03:00:30.200Z", (1, 999)),
            self._codex_token_count("2026-10-02T03:00:30.300Z", (300, 1300),
                                    (200, 700)),
        ]), [8_500, 10_299])

    def test_codex_latency_does_not_inherit_an_aborted_request_start(self) -> None:
        self.assertEqual(self._codex_durations([
            self._codex_response_item("2026-10-02T03:00:01.000Z", "message", "user"),
            self._codex_response_item("2026-10-02T03:00:10.000Z", "reasoning"),
            {"timestamp": "2026-10-02T03:00:20.000Z", "type": "event_msg",
             "payload": {"type": "turn_aborted", "reason": "interrupted"}},
            {"timestamp": "2026-10-02T03:02:00.000Z", "type": "event_msg",
             "payload": {"type": "task_started"}},
            self._codex_response_item("2026-10-02T03:02:00.010Z", "message", "user"),
            self._codex_response_item("2026-10-02T03:02:20.000Z", "reasoning"),
            self._codex_response_item("2026-10-02T03:02:30.000Z", "message",
                                      "assistant"),
            self._codex_token_count("2026-10-02T03:02:30.100Z", (100, 600),
                                    (100, 600)),
            # 响应中途插进来的 developer 消息不是新一轮,起点不动
            self._codex_response_item("2026-10-02T03:02:35.000Z", "reasoning"),
            self._codex_response_item("2026-10-02T03:02:36.000Z", "message",
                                      "developer"),
            self._codex_response_item("2026-10-02T03:02:40.000Z", "message",
                                      "assistant"),
            self._codex_token_count("2026-10-02T03:02:40.200Z", (300, 1300),
                                    (200, 700)),
        ]), [30_090, 10_100])

    def test_codex_latency_ignores_items_written_after_their_token_count(self) -> None:
        # 旧布局把输出项与工具结果补写在它们那次响应的 token_count 之后
        self.assertEqual(self._codex_durations([
            self._codex_response_item("2025-10-30T08:00:01.000Z", "message", "user"),
            self._codex_token_count("2025-10-30T08:00:08.000Z", (100, 600),
                                    (100, 600)),
            self._codex_response_item("2025-10-30T08:00:08.000Z", "reasoning"),
            self._codex_response_item("2025-10-30T08:00:08.001Z", "custom_tool_call"),
            self._codex_response_item("2025-10-30T08:00:08.001Z",
                                      "custom_tool_call_output"),
            {"timestamp": "2025-10-30T08:00:08.002Z", "type": "turn_context",
             "payload": {"model": "gpt-5"}},
            # 用户隔了一分多钟才发下一条消息,这段空闲不算进请求耗时
            self._codex_response_item("2025-10-30T08:01:40.000Z", "message", "user"),
            self._codex_token_count("2025-10-30T08:01:50.000Z", (300, 1300),
                                    (200, 700)),
        ]), [7_000, 10_000])

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

    def test_dsh_v4_repair_supersedes_frozen_v3_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            session_dir = sessions / "project" / "repaired"
            session_dir.mkdir(parents=True)

            def header(version: int) -> dict:
                return {"type": "session", "version": version, "id": "repaired",
                        "isSeeded": False, "cwd": "/work/project"}

            def user(seq: int, message_id: str, at: str) -> dict:
                return {"seq": seq, "type": "user/message", "time": at,
                        "data": {"id": message_id, "source": {"kind": "user"}}}

            def assistant(seq: int, message_id: str, value: int, at: str) -> dict:
                return {
                    "seq": seq, "type": "assistant/message", "time": at,
                    "data": {
                        "message": {"id": message_id,
                                    "source": {"model": "deepseek-v4-pro"}},
                        "usage": {"inputTokens": value, "cacheWriteTokens": 7,
                                   "cacheReadTokens": 200, "outputTokens": 50,
                                   "reasoningTokens": 20},
                    },
                }

            # DSH repairs an interrupted turn by publishing a verified V4
            # successor beside the unchanged V3 log, which freezes at the
            # migration point and stays there. V4 must win even though the
            # frozen V3 copy is the larger file.
            (session_dir / "session.v3.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    header(3),
                    user(0, "first-user", "2026-09-16T01:00:00Z"),
                    assistant(1, "first-assistant", 100, "2026-09-16T01:00:20Z"),
                    {"type": "assistant/chunk", "data": {"padding": "x" * 2000}},
                ]) + "\n", encoding="utf-8")
            # V4 lifts user-role tool results into tool-role messages, adds
            # developer/message, and adds forked/interrupted turn/end reasons;
            # none of those records carry billable usage.
            (session_dir / "session.v4.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    header(4),
                    {"type": "developer/message", "seq": 0,
                     "time": "2026-09-16T01:00:00Z",
                     "data": {"content": "instructions"}},
                    user(1, "first-user", "2026-09-16T01:00:00Z"),
                    assistant(2, "first-assistant", 100, "2026-09-16T01:00:20Z"),
                    {"type": "turn/end", "seq": 3,
                     "time": "2026-09-16T01:00:30Z",
                     "data": {"turn": 1, "reason": {"kind": "interrupted"}}},
                    {"type": "tool/message", "seq": 4,
                     "time": "2026-09-16T01:00:31Z",
                     "data": {"tool": "read", "content": "ok"}},
                    user(5, "second-user", "2026-09-16T02:00:00Z"),
                    assistant(6, "second-assistant", 300, "2026-09-16T02:00:20Z"),
                ]) + "\n", encoding="utf-8")

            sources = dsh.discover({"config": {"data_root": str(sessions)}})
            self.assertEqual([item.path.name for item in sources],
                             ["session.v4.jsonl"])
            parsed = dsh.parse(sources[0])
            self.assertFalse(parsed.skipped)
            self.assertEqual(
                [(event.prompt_tokens, event.completion_tokens,
                  event.cache_read_tokens) for event in parsed.events],
                [(307, 50, 200), (507, 50, 200)],
            )
            self.assertEqual(
                {event.project for event in parsed.events}, {"project"})
            self.assertEqual(
                {event.session_id for event in parsed.events}, {"repaired"})

    def test_dsh_v4_inherited_cut_matches_v3_parent_message_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            parent = sessions / "project-key" / "parent"
            child = sessions / "project-key" / "child"
            parent.mkdir(parents=True)
            child.mkdir(parents=True)

            def user(seq: int, message_id: str, at: str) -> dict:
                return {"seq": seq, "type": "user/message", "time": at,
                        "data": {"id": message_id, "source": {"kind": "user"}}}

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

            (parent / "session.v3.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    {"type": "session", "version": 3, "id": "parent",
                     "isSeeded": False, "cwd": "/work/parent"},
                    user(10, "u1", "2026-09-16T03:00:00Z"),
                    assistant(11, "a1", 100, "2026-09-16T03:00:20Z"),
                ]) + "\n", encoding="utf-8")
            (child / "session.v4.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    {"type": "session", "version": 4, "id": "child",
                     "parentSession": "parent", "isSeeded": True,
                     "cwd": "/work/child"},
                    user(20, "u1", "2026-09-16T03:10:00Z"),
                    assistant(21, "a1", 100, "2026-09-16T03:10:20Z"),
                    {"type": "session/end-seed", "seq": 22,
                     "time": "2026-09-16T03:10:21Z", "data": {"inherited": True}},
                    {"type": "turn/end", "seq": 23,
                     "time": "2026-09-16T03:10:22Z",
                     "data": {"turn": 1, "reason": {"kind": "forked"}}},
                    user(24, "u2", "2026-09-16T03:11:00Z"),
                    assistant(25, "a2", 300, "2026-09-16T03:11:20Z"),
                ]) + "\n", encoding="utf-8")

            sources = dsh.discover({"config": {"data_root": str(sessions)}})
            self.assertEqual(
                {item.path.name for item in sources},
                {"session.v3.jsonl", "session.v4.jsonl"},
            )
            skips = dsh.replay_skips(sources)
            child_source = next(item for item in sources
                                if item.path.parent.name == "child")
            self.assertEqual(skips[str(child_source.path)], 2)
            parsed = dsh.parse(
                child_source, skip_token_count=skips[str(child_source.path)])
            self.assertFalse(parsed.skipped)
            self.assertEqual([event.prompt_tokens for event in parsed.events],
                             [300])

    def test_dsh_rejects_format_version_above_v4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            session_dir = sessions / "project-key" / "future"
            session_dir.mkdir(parents=True)
            (session_dir / "session.v5.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    {"type": "session", "version": 5, "id": "future",
                     "isSeeded": False, "cwd": "/work/future"},
                    {"seq": 0, "type": "user/message",
                     "time": "2026-09-16T04:00:00Z",
                     "data": {"source": {"kind": "user"}}},
                ]) + "\n", encoding="utf-8")

            sources = dsh.discover({"config": {"data_root": str(sessions)}})
            self.assertEqual([item.path.name for item in sources],
                             ["session.v5.jsonl"])
            parsed = dsh.parse(sources[0])
            self.assertTrue(parsed.skipped)
            self.assertEqual(parsed.events, ())
            self.assertTrue(any("format version 5" in warning
                                for warning in parsed.warnings))

    def test_dsh_v4_session_reaches_request_log_through_import_once(self) -> None:
        """注册 dsh software 后,合成 V4 会话经 import_once 落入 request_log。"""
        from app.db.proxy_db import ProxyDatabase
        from app.db.schema_upgrade.coordinator import ensure_local_databases
        from app.services.agent_usage.importer import import_once

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            session_dir = sessions / "project-key" / "repaired"
            session_dir.mkdir(parents=True)
            (session_dir / "session.v4.jsonl").write_text(
                "\n".join(json.dumps(value) for value in [
                    {"type": "session", "version": 4, "id": "repaired",
                     "isSeeded": False, "cwd": "/work/project"},
                    {"seq": 0, "type": "user/message",
                     "time": "2026-09-16T05:00:00Z",
                     "data": {"id": "u1", "source": {"kind": "user"}}},
                    {"seq": 1, "type": "developer/message",
                     "time": "2026-09-16T05:00:01Z",
                     "data": {"content": "instructions"}},
                    {"seq": 2, "type": "assistant/message",
                     "time": "2026-09-16T05:00:20Z",
                     "data": {
                         "message": {"id": "a1",
                                     "source": {"model": "deepseek-v4-pro"}},
                         "usage": {"inputTokens": 100, "cacheWriteTokens": 7,
                                    "cacheReadTokens": 200, "outputTokens": 50,
                                    "reasoningTokens": 20},
                     }},
                ]) + "\n", encoding="utf-8")

            proxy_path = root / "data" / "token-board.db"
            dashboard_path = root / "data" / "dashboard.db"
            schema_root = PROJECT_ROOT / "schema"
            ensure_local_databases(
                str(proxy_path), str(dashboard_path), schema_root)
            proxy = ProxyDatabase(str(proxy_path), schema_dir=str(schema_root))
            proxy.create_agent_software({
                "name": "dsh-agent", "agent_kind": "dsh",
                "config": {"data_root": str(sessions)},
            })

            self.assertEqual(import_once(proxy), 1)
            with sqlite_connection(proxy_path) as conn:
                row = conn.execute(
                    "SELECT model,prompt_tokens,completion_tokens,"
                    "cache_read_tokens,total_tokens,requested_at "
                    "FROM request_log WHERE event_id LIKE 'dsh:%'"
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual((row[0], row[1], row[2], row[3], row[4]),
                             ("deepseek-v4-pro", 307, 50, 200, 357))
            self.assertTrue(str(row[5]).startswith("2026-09-16"))

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

    def _kimi_wire_fixture(self, root: Path, record: dict, *,
                           work_dir: str = "wd_project_hash",
                           session: str = "session-1",
                           agent: str = "main") -> Path:
        wire_dir = root / "sessions" / work_dir / session / "agents" / agent
        wire_dir.mkdir(parents=True, exist_ok=True)
        wire = wire_dir / "wire.jsonl"
        wire.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return wire

    def _kimi_usage_record(self, value: int) -> dict:
        return {"type": "usage.record", "model": "kimi-model",
                "time": 1782720000000,
                "usage": {"inputOther": value, "output": 1}}

    def _kimi_isolated_environ(self, directory: str) -> dict:
        # The legacy ~/.kimi store is always merged and its own env var wins
        # over the fixture hook; blank every locator so only the current store
        # registered in the test is scanned.
        fixture = str(Path(directory) / ".kimi")
        return {"KIMI_USAGE_LEGACY_ROOT": fixture,
                "VIBE_USAGE_KIMI_DIR": fixture,
                "KIMI_CODE_EXTRA_ROOTS": "",
                "VIBE_USAGE_KIMI_CODE_EXTRA_ROOTS": ""}

    def test_kimi_missing_root_is_a_normal_empty_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kimi-code"
            with patch.dict(os.environ, self._kimi_isolated_environ(directory)):
                self.assertEqual(
                    kimi_code.discover({"config": {"data_root": str(root)}}), [])

    def test_kimi_unreadable_store_level_is_reported_beside_readable_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kimi-code"
            blocked_wire = self._kimi_wire_fixture(root, self._kimi_usage_record(10))
            other_wire = self._kimi_wire_fixture(
                root, self._kimi_usage_record(20),
                work_dir="wd_other_hash", session="session-2")
            # wire.jsonl -> main / agents / session-1 / wd_project_hash / sessions
            agents = blocked_wire.parents[1]
            session_dir = blocked_wire.parents[2]
            work_dir = blocked_wire.parents[3]
            sessions = blocked_wire.parents[4]
            environment = self._kimi_isolated_environ(directory)

            with patch.dict(os.environ, environment):
                items = kimi_code.discover({"config": {"data_root": str(root)}})
            self.assertEqual({item.path for item in items},
                             {blocked_wire, other_wire})

            real_iterdir = Path.iterdir

            def fail_on(target: Path):
                def iterdir(self):
                    if self == target:
                        raise PermissionError(13, "Permission denied")
                    return real_iterdir(self)
                return iterdir

            for blocked, readable in (
                    (sessions, set()), (work_dir, {other_wire}),
                    (session_dir, {other_wire}), (agents, {other_wire})):
                with patch.object(Path, "iterdir", fail_on(blocked)), \
                        patch.dict(os.environ, environment):
                    items = kimi_code.discover(
                        {"config": {"data_root": str(root)}})
                reported = {item for item in items
                            if item.context.get("discovery_warnings")}
                self.assertEqual({item.path for item in items} - {item.path for item in reported},
                                 readable, blocked)
                self.assertEqual(len(reported), 1, blocked)
                warning_source = reported.pop()
                warnings = tuple(warning_source.context["discovery_warnings"])
                self.assertTrue(any(str(blocked) in warning
                                    for warning in warnings), blocked)
                # The unreadable level never becomes an empty success ...
                parsed = kimi_code.parse(warning_source)
                self.assertTrue(parsed.skipped, blocked)
                self.assertEqual(parsed.events, (), blocked)
                # ... while every readable store keeps importing.
                for path in readable:
                    source_item = next(item for item in items
                                       if item.path == path)
                    parsed = kimi_code.parse(source_item)
                    self.assertFalse(parsed.skipped, blocked)
                    self.assertEqual(len(parsed.events), 1, blocked)

    def test_kimi_unreadable_wire_file_is_skipped_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".kimi-code"
            blocked_wire = self._kimi_wire_fixture(
                root, self._kimi_usage_record(10), agent="main")
            other_wire = self._kimi_wire_fixture(
                root, self._kimi_usage_record(20), agent="agent-0")
            os.chmod(blocked_wire, 0o000)
            try:
                if os.access(blocked_wire, os.R_OK):
                    self.skipTest(
                        "filesystem permissions are not enforced for this user")
                with patch.dict(os.environ,
                                self._kimi_isolated_environ(directory)):
                    items = kimi_code.discover(
                        {"config": {"data_root": str(root)}})
                self.assertEqual({item.path for item in items},
                                 {blocked_wire, other_wire})
                by_path = {item.path: item for item in items}
                blocked = kimi_code.parse(by_path[blocked_wire])
                self.assertTrue(blocked.skipped)
                self.assertEqual(blocked.events, ())
                self.assertTrue(any("无法读取" in warning
                                    for warning in blocked.warnings))
                self.assertTrue(any(str(blocked_wire) in warning
                                    for warning in blocked.warnings))
                readable = kimi_code.parse(by_path[other_wire])
                self.assertFalse(readable.skipped)
                self.assertEqual(len(readable.events), 1)

                # The same read failure must also be reported when it happens
                # before any record has been read from the file.
                partial = kimi_code.parse(UsageSource(path=blocked_wire, context={
                    "layout": "current",
                    "session_dir": str(blocked_wire.parents[2]),
                    "root": str(root),
                }))
                self.assertTrue(partial.skipped)
                self.assertEqual(partial.events, ())
                self.assertTrue(any(str(blocked_wire) in warning
                                    for warning in partial.warnings))
            finally:
                os.chmod(blocked_wire, 0o600)

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
