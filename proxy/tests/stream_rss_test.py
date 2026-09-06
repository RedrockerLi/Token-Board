#!/usr/bin/env python3
"""Measure proxy RSS while one hundred streams are active."""

from __future__ import annotations

import concurrent.futures
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
from http.server import ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rss_bytes(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


class StressHTTPServer(ThreadingHTTPServer):
    # The default socket backlog is five; that makes a 100-stream RSS test
    # measure the Python fixture's accept queue instead of proxy memory.
    request_queue_size = 256


def wait_health(port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=0.3) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("proxy did not become healthy")


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    concurrency = int(os.environ.get("TOKEN_BOARD_RSS_CONCURRENCY", "100"))
    frames = int(os.environ.get("TOKEN_BOARD_RSS_FRAMES", "500"))
    sys.path.insert(0, str(project))
    from scripts.mock_upstream import Handler as MockHandler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = StressHTTPServer(("127.0.0.1", upstream_port), MockHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    try:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "token-board.db"
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                account, upstream_id, _ = add_upstream(
                    conn, "rss", f"http://127.0.0.1:{upstream_port}",
                    ["sk-rss"], max_concurrency=256)
                add_plain_route(conn, "tb-rss", "rss-route", upstream_id, account)
                conn.commit()
            finally:
                conn.close()

            port = free_port()
            env = os.environ.copy()
            env["TB_MAX_WORKERS"] = "128"
            proxy = subprocess.Popen(
                [str(binary), "--db", str(db), "--schema-dir", str(schema),
                 "--host", "127.0.0.1", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                env=env,
            )
            try:
                wait_health(port)
                baseline = rss_bytes(proxy.pid)
                body = json.dumps({
                    "model": "rss-model", "stream": True,
                    "mock_long_stream_frames": frames,
                    "mock_chunk_delay": 0.003,
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                barrier = threading.Barrier(concurrency)

                def request() -> int:
                    barrier.wait()
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        data=body,
                        headers={"Authorization": "Bearer tb-rss",
                                 "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=15) as response:
                        response.read()
                        return response.status

                with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = [pool.submit(request) for _ in range(concurrency)]
                    time.sleep(0.6)
                    peak = rss_bytes(proxy.pid)
                    statuses = [future.result() for future in futures]
                assert statuses == [200] * concurrency, statuses
                delta = peak - baseline
                assert delta <= 150 * 1024 * 1024, delta
                print(f"stream rss delta={delta / 1024 / 1024:.1f} MiB")
            finally:
                proxy.terminate()
                try:
                    proxy.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proxy.kill()
                    proxy.wait()
                if proxy.returncode not in (0, -15):
                    raise AssertionError(proxy.stderr.read())
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("stream RSS gate passed")


if __name__ == "__main__":
    main()
