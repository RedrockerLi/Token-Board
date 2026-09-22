#!/usr/bin/env python3
"""Verify downstream disconnects abort the stream and release its lease."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from support import sqlite_connection, stop_process


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=0.3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.HTTPError):
            time.sleep(0.05)
    raise AssertionError("proxy did not become healthy")


def wait_for_disconnect_log(db: Path, model: str) -> tuple[int, int, int, int]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with sqlite_connection(db) as conn:
            row = conn.execute(
                "SELECT status_code,prompt_tokens,completion_tokens,attempt_count "
                "FROM request_log WHERE model=? ORDER BY id DESC LIMIT 1",
                (model,),
            ).fetchone()
            if row is not None:
                attempt_count = conn.execute(
                    "SELECT count(*) FROM request_attempts a "
                    "JOIN request_log r ON r.id=a.request_log_id "
                    "WHERE r.model=?", (model,)
                ).fetchone()[0]
                return (*row, attempt_count)
        time.sleep(0.05)
    raise AssertionError("client-disconnect request was not logged")


def send_complete_request(port: int, key: str, body: dict) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()
        return response.status


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project))
    from scripts.mock_upstream import Handler as MockHandler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), MockHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = None
    try:
        with tempfile.TemporaryDirectory(prefix="token-board-disconnect-") as raw:
            root = Path(raw)
            db = root / "token-board.db"
            ensure_v2_database(db, schema)
            with sqlite_connection(db) as conn:
                account, upstream_id, _ = add_upstream(
                    conn, "disconnect", f"http://127.0.0.1:{upstream_port}",
                    ["sk-disconnect"], max_concurrency=1)
                add_plain_route(conn, "tb-disconnect", "disconnect-route",
                                upstream_id, account)
                conn.commit()

            proxy_port = free_port()
            env = os.environ.copy()
            env["TB_MAX_WORKERS"] = "8"
            proxy = subprocess.Popen(
                [str(binary), "--db", str(db), "--host", "127.0.0.1",
                 "--port", str(proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=env,
            )
            wait_for_health(proxy_port)

            model = "client-disconnect-model"
            body = json.dumps({
                "model": model,
                "stream": True,
                "mock_simple_stream": True,
                "mock_long_stream_frames": 80,
                "mock_chunk_delay": 0.03,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            client = socket.create_connection(("127.0.0.1", proxy_port), timeout=8)
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer tb-disconnect\r\n"
                b"Content-Type: application/json\r\n" +
                f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            received = b""
            while b"data:" not in received:
                chunk = client.recv(4096)
                if not chunk:
                    raise AssertionError("proxy closed stream before first event")
                received += chunk
            # RST the downstream connection so the server must observe the
            # failed sink write while the upstream is still streaming.
            client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()

            status, prompt, completion, attempts, attempt_rows = \
                wait_for_disconnect_log(db, model)
            assert (status, prompt, completion, attempts, attempt_rows) == \
                (499, 0, 0, 1, 1), (status, prompt, completion, attempts, attempt_rows)

            follow_up = send_complete_request(
                proxy_port, "tb-disconnect", {
                    "model": "after-disconnect",
                    "stream": False,
                    "messages": [{"role": "user", "content": "still alive"}],
                })
            assert follow_up == 200, follow_up
    finally:
        if proxy is not None:
            stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("client disconnect recovery passed")


if __name__ == "__main__":
    main()
