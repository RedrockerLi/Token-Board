#!/usr/bin/env python3
"""A full bounded task queue returns an HTTP 503 instead of resetting TCP."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import sqlite_connection, stop_process


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


def send(port: int, *, tolerate_body_write_race: bool = False) -> tuple[int, str | None]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps({"model": "slow", "stream": False,
                       "messages": [{"role": "user", "content": "hi"}]})
    try:
        conn.request("POST", "/v1/chat/completions", body,
                     {"Authorization": "Bearer tb-queue",
                      "Content-Type": "application/json"})
    except (BrokenPipeError, ConnectionResetError):
        # request() writes head and body as two socket writes.  A queue-full
        # 503 is written and the rejected socket shut down before the body is
        # read, so the body write can hit the closed connection.  The 503 is
        # still readable here: http.client marks the request as sent before
        # writing the body, which is all getresponse() below requires.
        if not tolerate_body_write_race:
            raise
    response = conn.getresponse()
    status, retry = response.status, response.getheader("Retry-After")
    response.read()
    conn.close()
    return status, retry


def send_two_phase(port: int) -> None:
    """Pin the product behaviour behind the tolerated write race in send().

    The client here deliberately loses the race: it writes the head in one
    call, waits until the queue-full 503 is visible on the socket, and only
    then dribbles the body — so the body write must eventually fail (the proxy
    shut the connection down) while the 503 must still be readable on that
    same connection.  If the product stops serving the 503 to a client that
    lost the write race, or stops failing the write, this case fails on
    purpose and the tolerance in send() has to be re-justified.
    """
    body = json.dumps({"model": "slow", "stream": False,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    head = (b"POST /v1/chat/completions HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{port}\r\n".encode()
            + b"Authorization: Bearer tb-queue\r\n"
              b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode())

    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.settimeout(2)
        # One write for the whole head: nothing can interleave with the
        # proxy's close here, unlike http.client's two-write request().
        sock.sendall(head)

        # The rejected connection is answered before the request body is read.
        # MSG_PEEK waits for the 503 without consuming the bytes read below.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            sock.settimeout(0.05)
            try:
                peeked = sock.recv(4096, socket.MSG_PEEK)
            except TimeoutError:
                continue
            except OSError:
                break
            if not peeked or b"HTTP/1.1 503" in peeked:
                break
            time.sleep(0.005)
        sock.settimeout(2)

        # Dribble the body one byte at a time; the proxy already closed this
        # connection, so a write must fail once the local stack sees the RST.
        write = "body-sent"
        for _ in range(30):
            try:
                sock.sendall(body[:1])
            except OSError as exc:
                write = f"body-{type(exc).__name__}"
                break
            time.sleep(0.02)

        # The 503 was already in the receive buffer when the writes started;
        # gather it up to the end of the header block.
        buffered = b""
        deadline = time.monotonic() + 2
        while b"\r\n\r\n" not in buffered and time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buffered += chunk

    assert write != "body-sent", (
        "writing a request body after the 503 must fail: the proxy is "
        "expected to have closed the rejected connection")
    status_line = buffered.split(b"\r\n", 1)[0]
    assert status_line == b"HTTP/1.1 503 Service Unavailable", buffered
    assert b"Retry-After: 1" in buffered, buffered


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project))
    from v1_fixture import ensure_v2_database
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
        db = Path(directory) / "token-board.db"
        ensure_v2_database(db, schema)
        with sqlite_connection(db) as conn:
            conn.executescript("""
                INSERT INTO accounts(id,uuid,name,account_kind) VALUES(1,'a','queue','proxy');
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
                INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,enabled)
                    VALUES('u',1,1,0,'sk-queue',1);
                INSERT INTO upstream_secrets(credential_uuid,secret_value)
                    VALUES('u','sk-queue');
            """)
        proxy_port = free_port()
        env = {**os.environ, "TB_MAX_WORKERS": "1", "TB_TASK_QUEUE_MAX": "1"}
        proxy = subprocess.Popen(
            [str(binary), "--db", str(db),
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
            assert send(proxy_port, tolerate_body_write_race=True) == (503, "1")
            send_two_phase(proxy_port)
            first.join(5); second.join(5)
            assert first_result == [(200, None)] and second_result == [(200, None)]
        finally:
            stop_process(proxy, timeout=5)
    upstream.shutdown(); upstream.server_close(); thread.join(timeout=2)
    print("queue saturation returned 503 with Retry-After")


if __name__ == "__main__":
    main()
