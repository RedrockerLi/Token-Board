#!/usr/bin/env python3
"""Verify success accounting uses upstream model metadata with route fallback."""

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

from support import stop_process


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
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def send(proxy_port: int, key: str, path: str, body: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
            assert response.status == 200, response.status
    except urllib.error.HTTPError as error:
        error.read()
        raise AssertionError(f"proxy returned HTTP {error.code}") from error


def latest_log(db_path: Path, previous_id: int) -> tuple[int, str, int, int]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT id,model,is_streaming,status_code FROM request_log "
                "WHERE id>? ORDER BY id LIMIT 1", (previous_id,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return row
        time.sleep(0.03)
    raise AssertionError("request-log row never appeared")


def main() -> None:
    proxy_binary = Path(sys.argv[1]).resolve()
    schema_dir = Path(sys.argv[2]).resolve()
    project_root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project_root))

    from scripts.mock_upstream import Handler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    upstream_thread = threading.Thread(
        target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    try:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "token-board.db"
            ensure_v2_database(db_path, schema_dir)
            conn = sqlite3.connect(db_path)
            try:
                account_id, upstream_id, _ = add_upstream(
                    conn, "model-accounting",
                    f"http://127.0.0.1:{upstream_port}",
                    ["sk-model-accounting"], api_format="openai")
                route_set_id = add_plain_route(
                    conn, "tb-model-accounting", "model-accounting-route",
                    upstream_id, account_id)
                conn.execute(
                    "UPDATE route_rules SET target_model=? WHERE route_set_id=?",
                    ("routed-model", route_set_id),
                )
                conn.commit()
            finally:
                conn.close()

            proxy_port = free_port()
            proxy = subprocess.Popen(
                [str(proxy_binary), "--db", str(db_path), "--host", "127.0.0.1",
                 "--port", str(proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=os.environ.copy())
            try:
                wait_for_health(proxy_port)
                previous_id = 0

                send(proxy_port, "tb-model-accounting", "/v1/chat/completions", {
                    "model": "client-model", "stream": False,
                    "mock_response_model": "server-nonstream-model",
                    "messages": [{"role": "user", "content": "hi"}],
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("server-nonstream-model", 0, 200), row
                previous_id = row[0]

                send(proxy_port, "tb-model-accounting", "/v1/chat/completions", {
                    "model": "client-model", "stream": False,
                    "mock_omit_model": True,
                    "messages": [{"role": "user", "content": "hi"}],
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("routed-model", 0, 200), row
                previous_id = row[0]

                send(proxy_port, "tb-model-accounting", "/v1/chat/completions", {
                    "model": "client-model", "stream": True,
                    "mock_response_model": "server-stream-model",
                    "messages": [{"role": "user", "content": "hi"}],
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("server-stream-model", 1, 200), row
                previous_id = row[0]

                send(proxy_port, "tb-model-accounting", "/v1/chat/completions", {
                    "model": "client-model", "stream": True,
                    "mock_omit_model": True,
                    "messages": [{"role": "user", "content": "hi"}],
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("routed-model", 1, 200), row
                previous_id = row[0]

                send(proxy_port, "tb-model-accounting", "/v1/embeddings", {
                    "model": "client-model",
                    "mock_response_model": "server-embedding-model",
                    "input": "hello",
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("server-embedding-model", 0, 200), row
                previous_id = row[0]

                send(proxy_port, "tb-model-accounting", "/v1/embeddings", {
                    "model": "client-model", "mock_omit_model": True,
                    "input": "hello",
                })
                row = latest_log(db_path, previous_id)
                assert row[1:] == ("routed-model", 0, 200), row
            finally:
                stop_process(proxy, timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)

    print("model accounting passed")


if __name__ == "__main__":
    main()
