#!/usr/bin/env python3
"""Sustained usage-accounting soak with an exactly-once final tally."""

from __future__ import annotations

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
from http.server import ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StressHTTPServer(ThreadingHTTPServer):
    request_queue_size = 256


def wait_health(port: int) -> dict:
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            if error.code != 503:
                raise
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("proxy health endpoint unavailable")


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    duration = float(os.environ.get("TOKEN_BOARD_SOAK_SECONDS", "60"))
    workers = int(os.environ.get("TOKEN_BOARD_SOAK_WORKERS", "64"))
    sys.path.insert(0, str(project))
    from app.db.migrations import migrate
    from scripts.mock_upstream import Handler as MockHandler
    from v1_fixture import add_plain_route, add_upstream

    upstream_port = free_port()
    upstream = StressHTTPServer(("127.0.0.1", upstream_port), MockHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    try:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "token-board.db"
            migrate(str(db), str(schema), "token-board")
            conn = sqlite3.connect(db)
            try:
                account, upstream_id, _ = add_upstream(
                    conn, "soak", f"http://127.0.0.1:{upstream_port}",
                    ["sk-soak"], max_concurrency=256)
                add_plain_route(conn, "tb-soak", "soak-route", upstream_id, account)
                conn.commit()
            finally:
                conn.close()

            port = free_port()
            env = os.environ.copy()
            env["TB_MAX_WORKERS"] = str(max(workers * 2, 128))
            proxy = subprocess.Popen(
                [str(binary), "--db", str(db), "--schema-dir", str(schema),
                 "--host", "127.0.0.1", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                env=env,
            )
            try:
                wait_health(port)
                body = json.dumps({
                    "model": "soak-model", "stream": False,
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                headers = {
                    "Authorization": "Bearer tb-soak",
                    "Content-Type": "application/json",
                    "Connection": "keep-alive",
                }
                end = time.monotonic() + duration
                counters = {"accepted": 0, "errors": 0}
                counter_lock = threading.Lock()

                def worker() -> None:
                    conn = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=10)
                    try:
                        while time.monotonic() < end:
                            try:
                                conn.request("POST", "/v1/chat/completions",
                                             body, headers)
                                response = conn.getresponse()
                                response.read()
                                with counter_lock:
                                    if response.status == 200:
                                        counters["accepted"] += 1
                                    else:
                                        counters["errors"] += 1
                            except (OSError, http.client.HTTPException):
                                with counter_lock:
                                    counters["errors"] += 1
                                conn.close()
                                conn = http.client.HTTPConnection(
                                    "127.0.0.1", port, timeout=10)
                    finally:
                        conn.close()

                threads = [threading.Thread(target=worker, daemon=True)
                           for _ in range(workers)]
                started = time.monotonic()
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                elapsed = max(time.monotonic() - started, 0.001)

                deadline = time.monotonic() + 15
                final_health = {}
                request_count = attempt_count = 0
                while time.monotonic() < deadline:
                    final_health = wait_health(port)
                    conn = sqlite3.connect(db)
                    try:
                        request_count = conn.execute(
                            "SELECT count(*) FROM request_log "
                            "WHERE model='soak-model'"
                        ).fetchone()[0]
                        attempt_count = conn.execute(
                            "SELECT count(*) FROM request_attempts t "
                            "JOIN request_log r ON r.id=t.request_log_id "
                            "WHERE r.model='soak-model'"
                        ).fetchone()[0]
                    finally:
                        conn.close()
                    accounting = final_health.get("accounting", {})
                    if (request_count == counters["accepted"] == attempt_count and
                            accounting.get("queue_depth", 0) == 0 and
                            accounting.get("spool_bytes", 0) == 0):
                        break
                    time.sleep(0.1)

                assert counters["accepted"] == request_count, (
                    counters, request_count)
                assert request_count == attempt_count, (request_count, attempt_count)
                assert final_health.get("accounting", {}).get("queue_depth", 0) == 0
                assert final_health.get("accounting", {}).get("spool_bytes", 0) == 0
                throughput = counters["accepted"] / elapsed
                assert throughput >= 1000, throughput
                print(f"soak accepted={counters['accepted']} "
                      f"errors={counters['errors']} rps={throughput:.1f}")
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait()
                if proxy.returncode not in (0, -15):
                    raise AssertionError(proxy.stderr.read())
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("accounting soak passed")


if __name__ == "__main__":
    main()
