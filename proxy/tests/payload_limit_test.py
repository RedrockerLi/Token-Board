#!/usr/bin/env python3
"""Verify the HTTP request-body limit returns 413 and preserves service health."""

from __future__ import annotations

import json
import os
import socket
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
        with tempfile.TemporaryDirectory(prefix="token-board-payload-") as raw:
            db = Path(raw) / "token-board.db"
            ensure_v2_database(db, schema)
            with sqlite_connection(db) as conn:
                account, upstream_id, _ = add_upstream(
                    conn, "payload", f"http://127.0.0.1:{upstream_port}",
                    ["sk-payload"], max_concurrency=1)
                add_plain_route(conn, "tb-payload", "payload-route",
                                upstream_id, account)
                conn.commit()

            proxy_port = free_port()
            proxy = subprocess.Popen(
                [str(binary), "--db", str(db), "--host", "127.0.0.1",
                 "--port", str(proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=os.environ.copy(),
            )
            wait_for_health(proxy_port)

            oversized = b"{" + b"\"x\":\"" + \
                b"a" * (32 * 1024 * 1024) + b"\"}"
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                data=oversized,
                headers={"Authorization": "Bearer tb-payload",
                         "Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(request, timeout=15)
            except urllib.error.HTTPError as error:
                assert error.code == 413, error.code
                error.read()
            else:
                raise AssertionError("oversized request was not rejected")

            valid = json.dumps({
                "model": "after-payload-limit",
                "stream": False,
                "messages": [{"role": "user", "content": "ok"}],
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                data=valid,
                headers={"Authorization": "Bearer tb-payload",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=8) as response:
                assert response.status == 200, response.status
                response.read()
    finally:
        if proxy is not None:
            stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("payload limit passed")


if __name__ == "__main__":
    main()
