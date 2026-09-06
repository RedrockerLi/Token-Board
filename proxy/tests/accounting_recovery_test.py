#!/usr/bin/env python3
"""Crash-recovery regression for the durable request-log spool."""

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
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_proxy(port: int, *, allow_degraded: bool = False) -> None:
    deadline = time.monotonic() + 8
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.3) as response:
                if response.status == 200:
                    return
        except urllib.error.HTTPError as error:
            if not allow_degraded or error.code != 503:
                raise
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("proxy did not become healthy")


def start_proxy(binary: Path, db: Path, schema: Path, port: int, env: dict):
    return subprocess.Popen(
        [str(binary), "--db", str(db), "--schema-dir", str(schema),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
    )


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
    try:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "token-board.db"
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                account, upstream_id, _ = add_upstream(
                    conn, "recovery", f"http://127.0.0.1:{upstream_port}",
                    ["sk-recovery"], max_concurrency=8)
                add_plain_route(conn, "tb-recovery", "recovery-route",
                                upstream_id, account)
                conn.commit()
            finally:
                conn.close()

            first_port = free_port()
            crash_env = os.environ.copy()
            crash_env["TB_TEST_CRASH_AFTER_SPOOL_SYNC"] = "1"
            first = start_proxy(binary, db, schema, first_port, crash_env)
            try:
                wait_for_proxy(first_port)
                body = json.dumps({
                    "model": "recovery-model", "stream": False,
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{first_port}/v1/chat/completions",
                    data=body,
                    headers={"Authorization": "Bearer tb-recovery",
                             "Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        response.read()
                except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                    # The injected _exit may race the response write; the
                    # durable spool is the contract under test.
                    pass
                deadline = time.monotonic() + 8
                while first.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert first.returncode == 86, first.returncode
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait()

            spool = Path(str(db) + ".request-log.spool")
            assert spool.exists() and spool.stat().st_size > 0

            second_port = free_port()
            second = start_proxy(binary, db, schema, second_port, os.environ.copy())
            try:
                wait_for_proxy(second_port, allow_degraded=True)
                request_count = attempt_count = 0
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    conn = sqlite3.connect(db)
                    try:
                        request_count = conn.execute(
                            "SELECT count(*) FROM request_log "
                            "WHERE model='recovery-model'"
                        ).fetchone()[0]
                        attempt_count = conn.execute(
                            "SELECT count(*) FROM request_attempts t "
                            "JOIN request_log r ON r.id=t.request_log_id "
                            "WHERE r.model='recovery-model'"
                        ).fetchone()[0]
                    finally:
                        conn.close()
                    if request_count == 1 and attempt_count == 1:
                        break
                    time.sleep(0.05)
                assert request_count == 1, request_count
                assert attempt_count == 1, attempt_count
                wait_for_proxy(second_port)
            finally:
                second.terminate()
                try:
                    second.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    second.kill()
                    second.wait()
                if second.returncode not in (0, -15):
                    raise AssertionError(second.stderr.read())
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("accounting recovery passed")


if __name__ == "__main__":
    main()
