#!/usr/bin/env python3
"""A full bounded task queue returns an HTTP 503 instead of resetting TCP."""

from __future__ import annotations

import http.client
import importlib.util
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


started = threading.Event()


class SlowHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        started.set()
        time.sleep(1.0)
        body = json.dumps({
            "id": "slow", "choices": [{"message": {"role": "assistant",
                                                        "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int) -> None:
    for _ in range(150):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.2):
                return
        except OSError:
            time.sleep(.02)
    raise AssertionError("proxy did not become healthy")


def send(port: int) -> tuple[int, str | None]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps({"model": "slow", "stream": False,
                       "messages": [{"role": "user", "content": "hi"}]})
    conn.request("POST", "/v1/chat/completions", body,
                 {"Authorization": "Bearer tb-queue",
                  "Content-Type": "application/json"})
    response = conn.getresponse()
    status, retry = response.status, response.getheader("Retry-After")
    response.read()
    conn.close()
    return status, retry


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    spec = importlib.util.spec_from_file_location(
        "queue_test_migrations", project / "app/db/migrations.py")
    assert spec is not None and spec.loader is not None
    migrations = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = migrations
    spec.loader.exec_module(migrations)

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), SlowHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "proxy.db"
        migrations.migrate(str(db), str(schema), "proxy")
        with sqlite3.connect(db) as conn:
            conn.executescript("""
                INSERT INTO accounts(id,uuid,name) VALUES(1,'a','queue');
                INSERT INTO route_sets(id,uuid,account_id,name) VALUES(1,'r',1,'queue');
                INSERT INTO client_keys(id,uuid,key_value,label,route_set_id)
                    VALUES(1,'c','tb-queue','queue',1);
                INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,valid_from)
                    VALUES('b',1,'metered','account','2020-01-01');
            """)
            conn.execute(
                "INSERT INTO upstreams(id,account_id,name,base_url,max_concurrency) "
                "VALUES(1,1,'slow',?,8)", (f"http://127.0.0.1:{upstream_port}",))
            conn.executescript("""
                INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id)
                    VALUES(1,'*',0,1);
                INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked)
                    VALUES('u',1,1,0,'sk-queue');
                INSERT INTO upstream_secrets(credential_uuid,secret_value)
                    VALUES('u','sk-queue');
            """)
        proxy_port = free_port()
        env = {**os.environ, "TB_MAX_WORKERS": "1", "TB_TASK_QUEUE_MAX": "1"}
        proxy = subprocess.Popen(
            [str(binary), "--db", str(db), "--schema-dir", str(schema),
             "--host", "127.0.0.1", "--port", str(proxy_port),
             "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
        try:
            wait_health(proxy_port)
            first_result: list[tuple[int, str | None]] = []
            first = threading.Thread(target=lambda: first_result.append(send(proxy_port)))
            first.start()
            assert started.wait(2)
            second_result: list[tuple[int, str | None]] = []
            second = threading.Thread(target=lambda: second_result.append(send(proxy_port)))
            second.start()
            time.sleep(.1)
            assert send(proxy_port) == (503, "1")
            first.join(5); second.join(5)
            assert first_result == [(200, None)] and second_result == [(200, None)]
        finally:
            proxy.terminate()
            proxy.wait(timeout=5)
    upstream.shutdown(); upstream.server_close(); thread.join(timeout=2)
    print("queue saturation returned 503 with Retry-After")


if __name__ == "__main__":
    main()
