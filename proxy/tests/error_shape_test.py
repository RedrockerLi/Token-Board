#!/usr/bin/env python3
"""Lock the unified terminal-error rendering across chat/embeddings/models.

Regression gate for the C audit finding: the three non-streaming handlers and
the streaming tail used to render failure bodies three different ways (chat via
harness_codec.serialize_error_body + parse_error_body, embeddings/models via
json_error with a raw "Upstream error: ..." text).  After the refactor every
endpoint routes through the shared render_terminal_error(), so the error JSON
shape is uniform:

  * fail-all busy 429   -> {"error": {"message", "type":"rate_limit_error", "code":429}}
  * upstream timeout    -> 504 + Connection: close + {"error":{...,"type":"timeout_error","code":504}}
  * used upstream error -> the structured parse_error_body output (not a
                           synthetic "Upstream error: <raw>" text)
  * Anthropic clients   -> the {"type":"error","error":{...}} envelope

The busy-429 branches need zero attempts.  Because AccountGate treats
max_concurrency<=0 as unlimited, the busy state is reached the proven way
(fallback_matrix A3): a recurring key that answered 429 with the real
GoUsageLimitError envelope enters the 5h cooldown; the next request then finds
every candidate cooling and never contacts the upstream.

Usage:
  python3 error_shape_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from http.server import ThreadingHTTPServer


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                        timeout=0.2) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def send(proxy_port: int, method: str, path: str, local_key: str,
         body: dict | None, timeout: float = 20) -> tuple[int, dict, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}{path}",
        data=data, method=method,
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        parsed = json.loads(raw) if raw else {}
        return e.code, parsed, e.headers


def send_stream(proxy_port: int, path: str, local_key: str,
                body: dict, timeout: float = 20) -> tuple[int, bytes, object]:
    """Send a streaming request while keeping the downstream connection open."""
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=timeout)
    try:
        conn.request(
            "POST", path, body=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {local_key}",
                     "Content-Type": "application/json",
                     "Connection": "keep-alive"})
        response = conn.getresponse()
        return response.status, response.read(), response.headers
    finally:
        conn.close()


def ctrl(upstream_port: int, payload: dict) -> None:
    """Toggle the mock's out-of-band per-key GET status map (/__ctrl)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{upstream_port}/__ctrl",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def count_rows(db_path: str, model: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        # Writer is async; poll briefly.
        for _ in range(100):
            n = conn.execute(
                "SELECT COUNT(*) FROM request_log WHERE model=?",
                (model,),
            ).fetchone()[0]
            if n:
                return n
            time.sleep(0.05)
        return 0
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token_proxy", type=str)
    ap.add_argument("schema_dir", type=str)
    ap.add_argument("project_root", type=str)
    args = ap.parse_args()

    proxy_binary = Path(args.token_proxy).resolve()
    schema_dir = Path(args.schema_dir).resolve()
    project_root = Path(args.project_root).resolve()
    sys.path.insert(0, str(project_root))
    from app.db.migrations import migrate
    from scripts.mock_upstream import Handler

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "token-board.db"
        migrate(str(db_path), str(schema_dir), "token-board")
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            from v1_fixture import add_plain_route, add_upstream

            def plain(key: str, name: str, *,
                      recurring: bool = False, api_format: str = "openai"):
                account_id, up, _ = add_upstream(
                    conn, name, base_url, [key], recurring=recurring,
                    api_format=api_format)
                add_plain_route(conn, f"tb-{name}", name, up, account_id,
                                model_pattern="*")

            # Separate accounts so the cooldown state of one scenario can never
            # leak into another.
            plain("sk-chat-500", "chat500")
            plain("sk-chat-to", "chatto")
            plain("sk-chat-stream", "chatstream")
            plain("sk-chat-busy", "chatbusy", recurring=True)
            plain("sk-anth-busy", "anthbusy", recurring=True, api_format="anthropic")
            plain("sk-emb-500", "emb500")
            plain("sk-emb-busy", "embbusy", recurring=True)
            plain("sk-mod-500", "mod500")
            plain("sk-mod-busy", "modbusy", recurring=True)

            # Fast timeout round-trip for the chat timeout case.
            conn.execute(
                "UPDATE proxy_timeout_config SET non_streaming_timeout=1 "
                "WHERE endpoint_kind='chat'")
            conn.execute(
                "UPDATE proxy_timeout_config SET streaming_first_byte_timeout=1 "
                "WHERE endpoint_kind='chat'")
            conn.execute(
                "UPDATE upstreams SET base_url=?", (base_url,))
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=os.environ.copy())
        try:
            wait_for_health(proxy_port)
            chat = {"messages": [{"role": "user", "content": "hi"}]}

            # ── 1. Chat OpenAI used-failure 500 → raw passthrough ──
            status, body, _ = send(proxy_port, "POST", "/v1/chat/completions",
                "tb-chat500", {**chat, "model": "chat500-model",
                               "mock_status_by_key": {"sk-chat-500": 500}})
            assert status == 500, f"chat 500 status {status}"
            assert body == {"error": {"message": "mock error",
                                      "type": "mock_error", "code": 500}}, body
            print(f"1 OK: chat OpenAI used-failure 500 passthrough -> {body}")

            # ── 2. Chat used-failure timeout → 504 + Connection: close ──
            status, body, headers = send(proxy_port, "POST",
                "/v1/chat/completions", "tb-chatto",
                {**chat, "model": "chatto-model", "mock_delay": 3})
            assert status == 504, f"chat timeout status {status}"
            assert headers.get("Connection") == "close", headers.get("Connection")
            err = body["error"]
            assert err["type"] == "timeout_error" and err["code"] == 504, body
            assert str(err["message"]).lower().startswith("upstream timeout"), body
            print(f"2 OK: chat timeout -> 504 + Connection: close + {err['type']}")

            # ── 3. Streaming first-byte timeout → 504 + Connection: close ──
            status, raw, headers = send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatstream",
                {**chat, "model": "chatstream-model", "stream": True,
                 "mock_delay": 3}, timeout=10)
            assert status == 504, (
                f"stream timeout status {status}: headers={headers}, raw={raw!r}")
            assert headers.get("Connection") == "close", headers
            assert headers.get("Keep-Alive") is None, headers
            assert b"timeout_error" in raw, raw
            print("3 OK: streaming timeout -> 504 + Connection: close")

            # ── 4. Streaming all-candidates-busy → 429 ──
            send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatbusy",
                {**chat, "model": "chatbusy-model", "stream": True,
                 "mock_status": 429, "mock_error_type": "GoUsageLimitError"},
                timeout=10)
            status, raw, headers = send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatbusy",
                {**chat, "model": "chatbusy-model", "stream": True},
                timeout=10)
            assert status == 429, (
                f"stream busy status {status}: headers={headers}, raw={raw!r}")
            assert b"rate_limit_error" in raw, raw
            print("4 OK: streaming all-candidates-busy -> 429")

            # ── 5. Anthropic chat busy-429 → Anthropic envelope ──
            # Req1: a recurring key answers 429 GoUsageLimitError → 5h cooldown.
            send(proxy_port, "POST", "/v1/messages", "tb-anthbusy",
                 {"model": "anthbusy-model", "messages": chat["messages"],
                  "mock_status": 429, "mock_error_type": "GoUsageLimitError"})
            status, body, _ = send(proxy_port, "POST", "/v1/messages",
                "tb-anthbusy", {"model": "anthbusy-model",
                                "messages": chat["messages"]})
            assert status == 429, f"anth busy status {status}"
            assert body == {"type": "error", "error": {
                "message": "All upstream accounts are busy, cooling down, or failed",
                "type": "rate_limit_error", "code": 429}}, body
            print(f"5 OK: Anthropic chat busy-429 envelope -> {body}")

            # ── 6. Embeddings used-failure 500 → structured, no "Upstream error:" ──
            status, body, _ = send(proxy_port, "POST", "/v1/embeddings",
                "tb-emb500", {"model": "emb500-model", "input": "hi",
                              "mock_status_by_key": {"sk-emb-500": 500}})
            assert status == 500, f"emb 500 status {status}"
            assert body == {"error": {"message": "mock error",
                                      "type": "mock_error", "code": 500}}, body
            assert "Upstream error" not in json.dumps(body)
            print(f"6 OK: embeddings used-failure 500 structured -> {body}")

            # ── 7. Embeddings busy-429 (recurring key cooled) ──
            send(proxy_port, "POST", "/v1/embeddings", "tb-embbusy",
                 {"model": "embbusy-model", "input": "hi",
                  "mock_status": 429, "mock_error_type": "GoUsageLimitError"})
            status, body, _ = send(proxy_port, "POST", "/v1/embeddings",
                "tb-embbusy", {"model": "embbusy-model", "input": "hi"})
            assert status == 429, f"emb busy status {status}"
            assert body == {"error": {"message":
                "All upstream accounts are busy, cooling down, or failed",
                "type": "rate_limit_error", "code": 429}}, body
            print(f"7 OK: embeddings busy-429 -> {body}")

            # ── 8. Models busy-429 (GET, out-of-band status) + no accounting ──
            ctrl(upstream_port, {"set": {"sk-mod-busy": 429}})
            send(proxy_port, "GET", "/v1/models", "tb-modbusy", None)
            ctrl(upstream_port, {"clear": ["sk-mod-busy"]})
            status, body, _ = send(proxy_port, "GET", "/v1/models",
                "tb-modbusy", None)
            assert status == 429, f"models busy status {status}"
            assert body == {"error": {"message":
                "All upstream keys are busy or cooling down",
                "type": "rate_limit_error", "code": 429}}, body
            assert count_rows(db_path, "modbusy-model") == 0, \
                "models must not write request_log rows"
            print(f"8 OK: models busy-429 (no accounting) -> {body}")

            # ── 9. Models used-failure 500 → structured upstream error ──
            ctrl(upstream_port, {"set": {"sk-mod-500": 500}})
            status, body, _ = send(proxy_port, "GET", "/v1/models",
                "tb-mod500", None)
            ctrl(upstream_port, {"clear": ["sk-mod-500"]})
            assert status == 500, f"models 500 status {status}"
            err = body["error"]
            assert err["message"] == "mock GoUsageLimitError", body
            assert err["type"] == "GoUsageLimitError", body
            assert "Upstream error" not in json.dumps(body)
            print(f"9 OK: models used-failure 500 structured -> {body}")

            print("error_shape tests passed")
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
            if proxy.returncode not in (0, -15):
                print(proxy.stderr.read(), file=sys.stderr)

    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
