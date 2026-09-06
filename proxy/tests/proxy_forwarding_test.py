#!/usr/bin/env python3
"""End-to-end regression for multi-key fallback before stream commitment."""

from __future__ import annotations

import json
import os
import shutil
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


BAD_AUTH = "bad-auth"
STREAM_ERROR = "stream-error"
GOOD = "good-key"


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        key = self.headers.get("Authorization", "").removeprefix("Bearer ")

        if key == BAD_AUTH:
            body = b'{"error":{"message":"revoked"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        if key == STREAM_ERROR:
            self.wfile.write(
                b"event: error\n"
                b'data: {"type":"error","error":{"type":"overloaded_error",'
                b'"message":"busy"}}\n\n'
            )
        else:
            frames = [
                ("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": "msg_ok", "type": "message", "role": "assistant",
                        "content": [], "model": "claude-test",
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                }),
                ("content_block_start", {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }),
                ("content_block_delta", {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                }),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                }),
                ("message_stop", {"type": "message_stop"}),
            ]
            for event, payload in frames:
                self.wfile.write(
                    f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
                )
        self.wfile.flush()
        self.close_connection = True


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 5
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def main() -> None:
    proxy_binary = Path(sys.argv[1]).resolve()
    schema_dir = Path(sys.argv[2]).resolve()
    project_root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project_root))
    from v1_fixture import ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), FakeUpstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        try:
            from v1_fixture import add_plain_route, add_upstream
            account_id, upstream_id, _ = add_upstream(
                conn, "test", f"http://127.0.0.1:{upstream_port}",
                [BAD_AUTH, STREAM_ERROR, GOOD], api_format="anthropic",
                auth_scheme="bearer", max_concurrency=8)
            local_key = "tb-test"
            add_plain_route(conn, local_key, "test-route", upstream_id,
                            account_id)
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            wait_for_health(proxy_port)
            body = json.dumps({
                "model": "claude-test", "max_tokens": 16, "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/messages",
                data=body,
                headers={"Authorization": f"Bearer {local_key}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response_body = response.read().decode()
            assert '"text":"ok"' in response_body.replace(" ", "")

            conn = sqlite3.connect(db_path)
            try:
                expected_version = max(
                    int(path.name.split("_", 1)[0].split("-")[0]) * 10000
                    + int(path.name.split("_", 1)[0].split("-")[1])
                    for path in (schema_dir / "token-board/v2").glob("*.sql"))
                assert conn.execute("PRAGMA user_version").fetchone()[0] == expected_version
                # Logging is now asynchronous (a dedicated accounting thread);
                # the HTTP response can complete before the row is durable, so
                # poll briefly for the proxy's row (attempt_count == 3 uniquely
                # identifies it vs. the legacy-orphan row inserted above).
                import time
                log = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    row = conn.execute(
                        "SELECT id,status_code,attempt_count FROM request_log "
                        "WHERE attempt_count = 3 ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        log = row
                        break
                    time.sleep(0.05)
                assert log is not None, "proxy request-log row never appeared"
                assert log[1:] == (200, 3), log
                statuses = [row[0] for row in conn.execute(
                    "SELECT status_code FROM request_attempts "
                    "WHERE request_log_id=? ORDER BY attempt_index", (log[0],)
                )]
                assert statuses == [401, 503, 200], statuses

                # V2 removes the live graph while retaining detached facts and
                # the stable historical identity.
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(
                    "UPDATE request_log SET account_id=NULL,route_set_id=NULL,"
                    "client_key_id=NULL,upstream_key_id=NULL,credential_uuid=NULL "
                    "WHERE id=?", (log[0],))
                conn.execute(
                    "UPDATE request_attempts SET account_id=NULL,upstream_id=NULL,"
                    "upstream_key_id=NULL,credential_uuid=NULL WHERE request_log_id=?",
                    (log[0],))
                conn.execute("DELETE FROM upstream_secrets WHERE credential_uuid IN "
                             "(SELECT c.uuid FROM upstream_credentials c JOIN upstreams u "
                             "ON u.id=c.upstream_id WHERE u.account_id=?)", (account_id,))
                conn.execute("DELETE FROM upstream_credentials WHERE upstream_id IN "
                             "(SELECT id FROM upstreams WHERE account_id=?)", (account_id,))
                conn.execute("DELETE FROM route_rules WHERE route_set_id IN "
                             "(SELECT id FROM route_sets WHERE account_id=?)", (account_id,))
                conn.execute("DELETE FROM client_keys WHERE route_set_id IN "
                             "(SELECT id FROM route_sets WHERE account_id=?)", (account_id,))
                conn.execute("DELETE FROM route_sets WHERE account_id=?", (account_id,))
                conn.execute("DELETE FROM upstreams WHERE account_id=?", (account_id,))
                conn.execute("DELETE FROM billing_contracts WHERE account_id=?", (account_id,))
                conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
                conn.commit()
                assert conn.execute(
                    "SELECT account_id FROM request_log WHERE id=?", (log[0],)
                ).fetchone()[0] is None
                assert conn.execute(
                    "SELECT account_identity_id FROM request_log WHERE id=?", (log[0],)
                ).fetchone()[0] == account_id
                assert all(row[0] is None for row in conn.execute(
                    "SELECT account_id FROM request_attempts WHERE request_log_id=?",
                    (log[0],),
                ))
                assert conn.execute(
                    "SELECT 1 FROM accounts WHERE id=?", (account_id,)
                ).fetchone() is None
            finally:
                conn.close()
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
    upstream_thread.join(timeout=2)
    print("proxy forwarding tests passed")


if __name__ == "__main__":
    main()
