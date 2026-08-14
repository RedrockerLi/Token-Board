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
                "body": body,
            })
        if self.path.endswith("/responses"):
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
            self._send_json({
                "id": "msg-converted", "type": "message", "role": "assistant",
                "model": body.get("model", "anthropic-model"),
                "content": [{"type": "text", "text": "anthropic-ok"}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 1},
            })
            return
        self.send_error(404)

    def _send_json(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
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


def post_path(port: int, key: str, path: str, body: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
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
    from app.db.migrations import migrate
    from v1_fixture import add_plain_route, add_upstream

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeResponsesUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "proxy.db"
        migrate(str(db), str(schema), "proxy")
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
            [str(binary), "--db", str(db), "--schema-dir", str(schema),
             "--host", "127.0.0.1", "--port", str(proxy_port),
             "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=os.environ.copy(),
        )
        try:
            wait_health(proxy_port)
            direct_body = {
                "model": "direct-model", "stream": False,
                "input": [{"role": "user", "content": "direct"}],
                "background": True,
            }
            status, direct_result = post(proxy_port, "tb-responses", direct_body)
            assert status == 200
            assert json.loads(direct_result)["output"][0]["content"][0]["text"] == "direct-ok"

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
            proxy.terminate()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
            if proxy.returncode not in (0, -15):
                raise AssertionError(proxy.stderr.read())
    upstream.shutdown()
    upstream.server_close()
    thread.join(timeout=2)
    print("Responses forwarding tests passed")


if __name__ == "__main__":
    main()
