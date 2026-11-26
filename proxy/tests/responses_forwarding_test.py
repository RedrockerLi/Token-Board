#!/usr/bin/env python3
"""Loopback coverage for /v1/responses passthrough and format conversion."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import stop_process

MINTED_ENVELOPE_PREFIXES = (
    "token-board-openai-reasoning-v1:",
    "token-board-anthropic-thinking-v1:",
)


def anthropic_blocks(body: dict) -> list[dict]:
    """Every content block of an Anthropic Messages request body."""
    blocks: list[dict] = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(part for part in content if isinstance(part, dict))
    return blocks


def fabricated_signature_field(body: dict) -> str | None:
    """Name of the first Anthropic signature/data field this service minted.

    Anthropic rejects thinking signatures it did not issue, so the mock
    upstream mimics that check: any value carrying our own envelope prefix is
    an automatic 400.
    """
    for index, block in enumerate(anthropic_blocks(body)):
        for field in ("signature", "data"):
            value = block.get(field)
            if isinstance(value, str) and value.startswith(MINTED_ENVELOPE_PREFIXES):
                return f"messages.{index}.{block.get('type', 'block')}.{field}"
    return None


class FakeResponsesUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict] = []
    lock = threading.Lock()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        with self.lock:
            self.requests.append({
                "path": self.path,
                "auth": self.headers.get("Authorization") or self.headers.get("x-api-key"),
                "anthropic_beta": self.headers.get("anthropic-beta"),
                "body": body,
            })
        if self.path.endswith("/responses/compact"):
            payload = {
                "id": "resp-compact",
                "object": "response.compaction",
                "created_at": 1,
                "status": "completed",
                "model": body.get("model", "compact-model"),
                "output": [{
                    "type": "compaction", "id": "compact-item",
                    "encrypted_content": "opaque-compaction-state",
                }],
            }
            self._send_json(payload)
            return
        if self.path.endswith("/responses"):
            if body.get("model") == "anthropic-reasoning-stream-text":
                # A reasoning Item WITH visible summary text: the Anthropic
                # emitter carries the text but must not attach a signature.
                response = {"id": "resp-reasoning-text", "object": "response",
                            "status": "completed",
                            "model": body.get("model", "responses-model"),
                            "output": []}
                frames = [
                    {"type": "response.created", "response": {
                        "id": response["id"], "object": "response",
                        "status": "in_progress", "model": response["model"]}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {"id": "rs-text", "type": "reasoning",
                              "status": "completed",
                              "summary": [{"type": "summary_text",
                                            "text": "visible plan"}]}},
                    {"type": "response.reasoning_summary_text.delta",
                     "output_index": 0, "item_id": "rs-text",
                     "summary_index": 0, "delta": "visible plan"},
                    {"type": "response.output_item.added", "output_index": 1,
                     "item": {"id": "msg-after", "type": "message",
                              "status": "in_progress", "role": "assistant",
                              "content": []}},
                    {"type": "response.output_text.delta", "output_index": 1,
                     "item_id": "msg-after", "content_index": 0,
                     "delta": "after-reasoning"},
                    {"type": "response.completed", "response": response},
                ]
                self._send_sse(("".join("data: " + json.dumps(frame) +
                                        "\n\n" for frame in frames)).encode())
                return
            if body.get("model") == "anthropic-reasoning-stream":
                # A reasoning Item with no summary text: the Anthropic stream
                # emitter has nothing legitimate to open for it.
                response = {"id": "resp-reasoning-stream", "object": "response",
                            "status": "completed",
                            "model": body.get("model", "responses-model"),
                            "output": []}
                frames = [
                    {"type": "response.created", "response": {
                        "id": response["id"], "object": "response",
                        "status": "in_progress", "model": response["model"]}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {"id": "rs-stream", "type": "reasoning",
                              "status": "completed", "summary": [],
                              "encrypted_content": "cipher"}},
                    {"type": "response.output_item.added", "output_index": 1,
                     "item": {"id": "msg-stream", "type": "message",
                              "status": "in_progress", "role": "assistant",
                              "content": []}},
                    {"type": "response.output_text.delta", "output_index": 1,
                     "item_id": "msg-stream", "content_index": 0,
                     "delta": "after-reasoning"},
                    {"type": "response.output_item.done", "output_index": 1,
                     "item": {"id": "msg-stream", "type": "message",
                              "status": "completed", "role": "assistant",
                              "content": [{"type": "output_text",
                                            "text": "after-reasoning",
                                            "annotations": []}]}},
                    {"type": "response.completed", "response": response},
                ]
                self._send_sse(("".join("data: " + json.dumps(frame) +
                                         "\n\n" for frame in frames)).encode())
                return
            if body.get("stream"):
                response = {
                    "id": "resp-stream",
                    "object": "response",
                    "status": "completed",
                    "model": body.get("model", "responses-model"),
                    "output": [{
                        "id": "msg-stream", "type": "message",
                        "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text",
                                      "text": "responses-stream-ok",
                                      "annotations": []}],
                    }],
                    "usage": {"input_tokens": 2, "output_tokens": 1,
                              "total_tokens": 3},
                }
                frames = [
                    {"type": "response.created", "response": {
                        "id": response["id"], "object": "response",
                        "status": "in_progress", "model": response["model"]}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": response["output"][0]},
                    {"type": "response.output_text.delta", "output_index": 0,
                     "item_id": "msg-stream", "content_index": 0,
                     "delta": "responses-stream-ok"},
                    {"type": "response.output_item.done", "output_index": 0,
                     "item": response["output"][0]},
                    {"type": "response.completed", "response": response},
                ]
                self._send_sse(("".join("data: " + json.dumps(frame) +
                                         "\n\n" for frame in frames)).encode())
                return
            payload = {
                "id": "resp-direct",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": body.get("model", "direct-model"),
                "output": [{
                    "id": "msg-direct", "type": "message", "status": "completed",
                    "role": "assistant", "content": [{
                        "type": "output_text", "text": "direct-ok", "annotations": []
                    }],
                }],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            }
            self._send_json(payload)
            return
        if self.path.endswith("/chat/completions"):
            if not body.get("stream"):
                payload = {
                    "id": "chat-converted", "object": "chat.completion",
                    "created": 1, "model": body.get("model", "chat-model"),
                    "choices": [{"index": 0,
                                  "message": {"role": "assistant",
                                              "content": "converted-ok"},
                                  "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                              "total_tokens": 18,
                              "prompt_tokens_details": {"cached_tokens": 3}},
                }
                self._send_json(payload)
                return
            frames = [
                {"id": "chat-converted", "object": "chat.completion.chunk",
                 "model": body.get("model", "chat-model"), "created": 1,
                 "choices": [{"index": 0, "delta": {"role": "assistant"},
                               "finish_reason": None}]},
                {"id": "chat-converted", "object": "chat.completion.chunk",
                 "model": body.get("model", "chat-model"), "created": 1,
                 "choices": [{"index": 0, "delta": {"content": "converted-ok"},
                               "finish_reason": None}]},
                {"id": "chat-converted", "object": "chat.completion.chunk",
                 "model": body.get("model", "chat-model"), "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
            ]
            data = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
            data += "data: [DONE]\n\n"
            self._send_sse(data.encode())
            return
        if self.path.endswith("/messages"):
            # P-17 regression guard: Anthropic validates thinking signatures,
            # so a value this service minted itself must be rejected exactly
            # like the real endpoint would (400 Invalid signature).  Nothing
            # in these tests may legitimately carry such a value.
            fabricated = fabricated_signature_field(body)
            if fabricated:
                self._send_json({
                    "type": "error",
                    "error": {"type": "invalid_request_error",
                              "message": f"{fabricated}: Invalid signature"},
                }, status=400)
                return
            if body.get("model") == "anthropic-upstream-error":
                self._send_json({
                    "type": "error",
                    "error": {"type": "invalid_request_error",
                              "message": "thinking.signature: Invalid signature"},
                }, status=400)
                return
            payload = {
                "id": "msg-converted", "type": "message", "role": "assistant",
                "model": body.get("model", "anthropic-model"),
                "content": [{"type": "text", "text": "anthropic-ok"}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 1},
            }
            if body.get("model") == "anthropic-context":
                payload["context_management"] = {
                    "applied_edits": [{"type": "clear_tool_uses_20250919"}]
                }
            self._send_json(payload)
            return
        self.send_error(404)

    def _send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.03)
    raise AssertionError("proxy did not become healthy")


def post(port: int, key: str, body: dict) -> tuple[int, str]:
    return post_path(port, key, "/v1/responses", body)


def post_path(port: int, key: str, path: str, body: dict,
              extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(root))
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeResponsesUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "token-board.db"
        ensure_v2_database(db, schema)
        conn = sqlite3.connect(db)
        try:
            base = f"http://127.0.0.1:{upstream_port}/v1/"
            responses_account, responses_upstream, _ = add_upstream(
                conn, "responses", base, ["sk-responses"],
                api_format="openai_responses", max_concurrency=8)
            chat_account, chat_upstream, _ = add_upstream(
                conn, "chat", base, ["sk-chat"],
                api_format="openai", max_concurrency=8)
            anthropic_account, anthropic_upstream, _ = add_upstream(
                conn, "anthropic", base, ["sk-anthropic"],
                api_format="anthropic", auth_scheme="x-api-key", max_concurrency=8)
            add_plain_route(conn, "tb-responses", "responses-route",
                            responses_upstream, responses_account)
            add_plain_route(conn, "tb-chat", "chat-route", chat_upstream,
                            chat_account)
            add_plain_route(conn, "tb-anthropic", "anthropic-route",
                            anthropic_upstream, anthropic_account)
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(binary), "--db", str(db),
             "--host", "127.0.0.1", "--port", str(proxy_port),
             "--log-level", "warn"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=os.environ.copy(),
        )
        diagnostics = ""
        try:
            wait_health(proxy_port)
            direct_body = {
                "model": "direct-model", "stream": False,
                "input": [{"role": "user", "content": "direct"}],
                "background": True,
                "context_management": [{"type": "compaction",
                                         "compact_threshold": 1000}],
            }
            status, direct_result = post(proxy_port, "tb-responses", direct_body)
            assert status == 200
            assert json.loads(direct_result)["output"][0]["content"][0]["text"] == "direct-ok"
            with FakeResponsesUpstream.lock:
                direct_request = next(item for item in reversed(FakeResponsesUpstream.requests)
                                      if item["auth"] == "Bearer sk-responses" and
                                      item["body"].get("model") == "direct-model")
            assert direct_request["body"] == direct_body

            compact_body = {
                "model": "compact-model", "stream": False,
                "input": [{"role": "user", "content": "compact"}],
            }
            status, compact_result = post_path(
                proxy_port, "tb-responses", "/v1/responses/compact", compact_body)
            assert status == 200, compact_result
            assert json.loads(compact_result)["object"] == "response.compaction"
            with FakeResponsesUpstream.lock:
                compact_request = next(item for item in reversed(FakeResponsesUpstream.requests)
                                       if item["auth"] == "Bearer sk-responses" and
                                       item["path"].endswith("/responses/compact"))
            assert compact_request["body"] == compact_body

            # A converted second Responses turn must receive the complete
            # cached Item chain, not an upstream previous_response_id.
            state_first = {
                "model": "state-first", "stream": False,
                "input": [{"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "first"}]}],
            }
            status, state_first_result = post(proxy_port, "tb-responses", state_first)
            assert status == 200
            state_id = json.loads(state_first_result)["id"]
            state_second = {
                "model": "state-second", "stream": False,
                "previous_response_id": state_id,
                "input": [{"type": "function_call_output", "call_id": "state-call",
                            "output": "result"}],
            }
            status, state_second_result = post(proxy_port, "tb-responses", state_second)
            assert status == 200, state_second_result
            with FakeResponsesUpstream.lock:
                state_requests = [item for item in FakeResponsesUpstream.requests
                                  if item["auth"] == "Bearer sk-responses" and
                                  item["body"].get("model") in {"state-first", "state-second"}]
            second_upstream = next(item for item in state_requests
                                   if item["body"].get("model") == "state-second")
            assert "previous_response_id" not in second_upstream["body"]
            assert [item.get("type") for item in second_upstream["body"]["input"]] == [
                "message", "message", "function_call_output"]

            before = len(FakeResponsesUpstream.requests)
            status, missing_result = post(proxy_port, "tb-responses", {
                "model": "missing-state", "stream": False,
                "previous_response_id": "does-not-exist",
                "input": "should fail",
            })
            assert status == 400 and "previous_response_not_found" in missing_result
            assert len(FakeResponsesUpstream.requests) == before

            converted_body = {
                "model": "chat-model", "stream": True,
                "input": [{"role": "user", "content": "hello"}],
                "context_management": [{"type": "compaction",
                                         "compact_threshold": 1000}],
                "tools": [{"type": "function", "name": "lookup",
                            "parameters": {"type": "object"}}],
                "tool_choice": {"type": "function", "name": "lookup"},
            }
            status, converted_result = post(proxy_port, "tb-chat", converted_body)
            assert status == 200
            assert "event: response.created" in converted_result
            assert "response.output_text.delta" in converted_result
            assert "converted-ok" in converted_result
            assert "event: response.completed" in converted_result

            with FakeResponsesUpstream.lock:
                requests = list(FakeResponsesUpstream.requests)
            direct = next(item for item in requests if item["auth"] == "Bearer sk-responses")
            assert direct["path"] == "/v1/responses", direct
            assert direct["body"] == direct_body, (direct["body"], direct_body)
            chat = next(item for item in requests if item["auth"] == "Bearer sk-chat")
            assert chat["path"] == "/v1/chat/completions", chat
            assert chat["body"]["messages"][0] == {"role": "user", "content": "hello"}
            assert "input" not in chat["body"]
            assert chat["body"]["tool_choice"] == {
                "type": "function", "function": {"name": "lookup"}}
            assert chat["body"]["tools"][0]["type"] == "function"
            assert "context_management" not in chat["body"]

            sentinel = "IMG" + ("X" * 113 * 1024)
            media_body = {
                "model": "chat-media", "stream": True,
                "input": [{"type": "function_call_output", "call_id": "c-media",
                            "output": [{"type": "input_text", "text": "ok"},
                                        {"type": "input_image",
                                         "image_url": "data:image/png;base64," + sentinel},
                                        {"type": "input_file",
                                         "file_data": "data:application/pdf;base64,FILE_SENTINEL",
                                         "filename": "out.pdf"},
                                        {"type": "input_audio",
                                         "input_audio": {"data": "AUDIO_SENTINEL",
                                                         "format": "wav"}}]}],
            }
            status, media_result = post_path(proxy_port, "tb-chat",
                                             "/v1/responses", media_body)
            assert status == 200, media_result
            with FakeResponsesUpstream.lock:
                media_req = next(item for item in reversed(FakeResponsesUpstream.requests)
                                 if item["auth"] == "Bearer sk-chat" and
                                 item["body"].get("model") == "chat-media")
            chat_messages = media_req["body"]["messages"]
            tool_messages = [m for m in chat_messages if m.get("role") == "tool"]
            assert tool_messages and sentinel not in json.dumps(tool_messages)
            user_parts = [p for m in chat_messages if m.get("role") == "user"
                          for p in (m.get("content") if isinstance(m.get("content"), list) else [])]
            assert {p.get("type") for p in user_parts} >= {"image_url", "file", "input_audio"}
            assert media_result.find(sentinel) == -1

            anthropic_media = {
                "model": "anthropic-media", "max_tokens": 20, "stream": True,
                "messages": [
                    {"role": "assistant", "content": [{"type": "tool_use",
                        "id": "a-media", "name": "inspect", "input": {}}]},
                    {"role": "user", "content": [{"type": "tool_result",
                        "tool_use_id": "a-media", "content": [{"type": "image",
                        "source": {"type": "base64", "media_type": "image/png",
                                   "data": "ANTHROPIC_IMAGE"}}]}]},
                ],
            }
            status, anthropic_result = post_path(proxy_port, "tb-chat",
                                                 "/v1/messages", anthropic_media)
            assert status == 200, anthropic_result
            with FakeResponsesUpstream.lock:
                converted_anthropic = next(item for item in reversed(FakeResponsesUpstream.requests)
                                           if item["auth"] == "Bearer sk-chat" and
                                           item["body"].get("model") == "anthropic-media")
            assert all(m.get("role") != "tool" or "ANTHROPIC_IMAGE" not in json.dumps(m)
                       for m in converted_anthropic["body"]["messages"])
            assert any(p.get("type") == "image_url"
                       for m in converted_anthropic["body"]["messages"]
                       if m.get("role") == "user" and isinstance(m.get("content"), list)
                       for p in m["content"])

            anthropic_context = {
                "model": "anthropic-context", "max_tokens": 20, "stream": False,
                "messages": [{"role": "user", "content": "keep going"}],
                "context_management": {
                    "edits": [{"type": "clear_tool_uses_20250919"}]
                },
            }
            status, context_result = post_path(
                proxy_port, "tb-anthropic", "/v1/messages", anthropic_context,
                {"anthropic-beta": "prompt-caching-2024-07-31"})
            assert status == 200, context_result
            with FakeResponsesUpstream.lock:
                context_request = next(item for item in reversed(FakeResponsesUpstream.requests)
                                       if item["auth"] == "sk-anthropic" and
                                       item["body"].get("model") == "anthropic-context")
            assert context_request["body"]["context_management"] == \
                anthropic_context["context_management"]
            assert context_request["anthropic_beta"] == \
                "prompt-caching-2024-07-31,context-management-2025-06-27"
            assert json.loads(context_result)["context_management"] == {
                "applied_edits": [{"type": "clear_tool_uses_20250919"}]
            }

            anthropic_to_chat_context = {
                "model": "anthropic-chat-context", "max_tokens": 20,
                "stream": True,
                "messages": [{"role": "user", "content": "keep going"}],
                "context_management": {
                    "edits": [{"type": "clear_tool_uses_20250919"}]
                },
            }
            status, context_chat_result = post_path(
                proxy_port, "tb-chat", "/v1/messages",
                anthropic_to_chat_context)
            assert status == 200, context_chat_result
            assert "anthropic-ok" not in context_chat_result
            with FakeResponsesUpstream.lock:
                context_chat_request = next(
                    item for item in reversed(FakeResponsesUpstream.requests)
                    if item["auth"] == "Bearer sk-chat" and
                    item["body"].get("model") == "anthropic-chat-context")
            assert "context_management" not in context_chat_request["body"]
            assert context_chat_request["anthropic_beta"] is None

            anthropic_to_responses_context = {
                "model": "anthropic-responses-context", "max_tokens": 20,
                "stream": True,
                "messages": [{"role": "user", "content": "keep going"}],
                "context_management": {
                    "edits": [{"type": "clear_tool_uses_20250919"}]
                },
            }
            status, context_responses_result = post_path(
                proxy_port, "tb-responses", "/v1/messages",
                anthropic_to_responses_context)
            assert status == 200, context_responses_result
            assert "message_start" in context_responses_result
            assert "responses-stream-ok" in context_responses_result
            with FakeResponsesUpstream.lock:
                context_responses_request = next(
                    item for item in reversed(FakeResponsesUpstream.requests)
                    if item["auth"] == "Bearer sk-responses" and
                    item["body"].get("model") == "anthropic-responses-context")
            assert "context_management" not in context_responses_request["body"]

            responses_to_anthropic = {
                "model": "anthropic-image", "stream": False,
                "input": [{"type": "function_call_output", "call_id": "r-image",
                            "output": [{"type": "image", "data": "RESP_IMAGE",
                                         "mimeType": "image/png"}]}],
            }
            status, result = post_path(proxy_port, "tb-anthropic", "/v1/responses",
                                       responses_to_anthropic)
            assert status == 200, result
            with FakeResponsesUpstream.lock:
                target_anthropic = next(item for item in reversed(FakeResponsesUpstream.requests)
                                        if item["auth"] == "sk-anthropic" and
                                        item["body"].get("model") == "anthropic-image")
            tool_content = target_anthropic["body"]["messages"][0]["content"][0]
            assert target_anthropic["body"]["messages"][0]["role"] == "user"
            assert tool_content["type"] == "tool_result"
            assert tool_content["content"][0]["type"] == "image"

            # P-17: replaying a Responses conversation that carries historical
            # reasoning must reach Anthropic as an *unsigned* thinking block.
            # The mock 400s any minted signature, so a 200 here also proves the
            # request no longer fabricates one.
            reasoning_replay = {
                "model": "anthropic-reasoning-replay", "stream": False,
                "input": [
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "plan"}]},
                    {"type": "reasoning", "id": "rs-replay",
                     "status": "completed", "encrypted_content": "cipher",
                     "summary": [{"type": "summary_text",
                                  "text": "visible plan"}]},
                    {"type": "message", "id": "msg-replay", "role": "assistant",
                     "content": [{"type": "output_text", "text": "answer"}]},
                ],
            }
            status, replay_result = post_path(proxy_port, "tb-anthropic",
                                              "/v1/responses", reasoning_replay)
            with FakeResponsesUpstream.lock:
                replay_request = next(
                    item for item in reversed(FakeResponsesUpstream.requests)
                    if item["auth"] == "sk-anthropic" and
                    item["body"].get("model") == "anthropic-reasoning-replay")
            assert status == 200, (replay_result, replay_request["body"])
            replay_blocks = anthropic_blocks(replay_request["body"])
            replay_thinking = [block for block in replay_blocks
                               if block.get("type") == "thinking"]
            assert len(replay_thinking) == 1, replay_blocks
            assert replay_thinking[0]["thinking"] == "visible plan", replay_thinking
            assert "signature" not in replay_thinking[0], replay_thinking
            assert not any(block.get("type") == "redacted_thinking"
                           for block in replay_blocks), replay_blocks
            assert "token-board" not in json.dumps(replay_request["body"])

            # A reasoning item that only carries encrypted_content has no text
            # carrier: the block is dropped (with a warning) instead of being
            # re-labelled as redacted_thinking with a minted data value.
            encrypted_only_replay = {
                "model": "anthropic-reasoning-dropped", "stream": False,
                "input": [
                    {"type": "reasoning", "id": "rs-encrypted",
                     "encrypted_content": "cipher"},
                    {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]},
                ],
            }
            status, dropped_result = post_path(
                proxy_port, "tb-anthropic", "/v1/responses", encrypted_only_replay)
            with FakeResponsesUpstream.lock:
                dropped_request = next(
                    item for item in reversed(FakeResponsesUpstream.requests)
                    if item["auth"] == "sk-anthropic" and
                    item["body"].get("model") == "anthropic-reasoning-dropped")
            assert status == 200, (dropped_result, dropped_request["body"])
            assert "token-board" not in json.dumps(dropped_request["body"])
            assert "redacted_thinking" not in json.dumps(dropped_request["body"])
            for message in dropped_request["body"]["messages"]:
                assert message["content"], dropped_request["body"]

            # Streaming variant (Anthropic client, Responses upstream): the
            # Anthropic emitter must not open a thinking block for a reasoning
            # Item that has no text carrier, because the only thing it could
            # put in `signature` would be a value this service invented.
            reasoning_stream = {
                "model": "anthropic-reasoning-stream", "max_tokens": 20,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
            status, stream_reasoning_result = post_path(
                proxy_port, "tb-responses", "/v1/messages", reasoning_stream)
            assert status == 200, stream_reasoning_result
            assert "after-reasoning" in stream_reasoning_result, \
                stream_reasoning_result
            assert "thinking" not in stream_reasoning_result, \
                stream_reasoning_result
            assert "token-board" not in stream_reasoning_result, \
                stream_reasoning_result

            # Streaming variant with visible summary text: the text survives,
            # the block is still unsigned.
            reasoning_stream_text = {
                "model": "anthropic-reasoning-stream-text", "max_tokens": 20,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
            status, stream_text_result = post_path(
                proxy_port, "tb-responses", "/v1/messages", reasoning_stream_text)
            assert status == 200, stream_text_result
            assert "visible plan" in stream_text_result, stream_text_result
            assert "thinking_delta" in stream_text_result, stream_text_result
            assert "signature" not in stream_text_result, stream_text_result
            assert "token-board" not in stream_text_result, stream_text_result

            # An upstream 4xx must reach the client as a readable 4xx carrying
            # the upstream message/type, never as an opaque 502.
            upstream_error_body = {
                "model": "anthropic-upstream-error", "stream": False,
                "input": [{"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "boom"}]}],
            }
            status, upstream_error_result = post_path(
                proxy_port, "tb-anthropic", "/v1/responses", upstream_error_body)
            assert status == 400, (status, upstream_error_result)
            assert "Invalid signature" in upstream_error_result, upstream_error_result
            assert "invalid_request_error" in upstream_error_result, upstream_error_result

            # An Anthropic client's non-streaming request routed to the OpenAI
            # upstream: the Anthropic wire reports input_tokens as the
            # exclusive net input (cache buckets are separate), so the
            # OpenAI prompt that includes the cached tokens must not be
            # mirrored verbatim (fixture: prompt 11, cached 3 → net 8).
            anthropic_chat_usage = {
                "model": "anthropic-chat-usage", "max_tokens": 20,
                "stream": False,
                "messages": [{"role": "user", "content": "usage"}],
            }
            status, usage_result = post_path(proxy_port, "tb-chat",
                                             "/v1/messages", anthropic_chat_usage)
            assert status == 200, usage_result
            usage = json.loads(usage_result)["usage"]
            assert usage["input_tokens"] == 8, usage
            assert usage["cache_read_input_tokens"] == 3, usage
            assert usage["output_tokens"] == 7, usage
            with FakeResponsesUpstream.lock:
                usage_request = next(item for item in reversed(FakeResponsesUpstream.requests)
                                     if item["auth"] == "Bearer sk-chat" and
                                     item["body"].get("model") == "anthropic-chat-usage")
            assert not usage_request["body"].get("stream"), usage_request["body"]

            cross_format_context = {
                "model": "anthropic-context-cross", "stream": False,
                "input": "must not be converted",
                "context_management": {
                    "edits": [{"type": "clear_tool_uses_20250919"}]
                },
            }
            before = len(FakeResponsesUpstream.requests)
            status, cross_result = post_path(
                proxy_port, "tb-anthropic", "/v1/responses", cross_format_context)
            assert status == 422 and "context_management" in cross_result
            assert len(FakeResponsesUpstream.requests) == before

            compact_cross_body = {
                "model": "compact-cross", "stream": False,
                "input": "must not be compacted by Chat",
            }
            before = len(FakeResponsesUpstream.requests)
            status, compact_cross_result = post_path(
                proxy_port, "tb-chat", "/v1/responses/compact", compact_cross_body)
            assert status == 422 and "responses_compact" in compact_cross_result
            assert len(FakeResponsesUpstream.requests) == before

            unsupported_audio = {
                "model": "anthropic-audio", "stream": False,
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": "AUDIO",
                                                                "format": "wav"}}
                ]}],
            }
            before = len(FakeResponsesUpstream.requests)
            status, unsupported_result = post_path(proxy_port, "tb-anthropic",
                                                   "/v1/responses", unsupported_audio)
            assert status == 422, (status, unsupported_result)
            assert len(FakeResponsesUpstream.requests) == before

            unsupported_file_url = {
                "model": "chat-file-url", "stream": False,
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_file", "file_url": "https://example.invalid/a.pdf"}
                ]}],
            }
            before = len(FakeResponsesUpstream.requests)
            status, file_url_result = post_path(proxy_port, "tb-chat",
                                                "/v1/responses", unsupported_file_url)
            assert status == 422, (status, file_url_result)
            assert len(FakeResponsesUpstream.requests) == before
        finally:
            diagnostics = stop_process(proxy)
        # Dropping a reasoning item is a lossy conversion, so it must be
        # reported with its reason instead of failing silently.
        assert "reasoning item has no text carrier" in diagnostics, \
            diagnostics[-2000:]
    upstream.shutdown()
    upstream.server_close()
    thread.join(timeout=2)
    print("Responses forwarding tests passed")


if __name__ == "__main__":
    main()
