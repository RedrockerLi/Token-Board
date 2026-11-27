#!/usr/bin/env python3
"""Verify downstream disconnects abort the request and record zero cost.

The proxy has four 499 exits.  This file pins all of them:

  D1  committed stream, the downstream write fails      stream_pipeline.cpp:572-584
  D2  stream cancelled before the first upstream byte   stream_pipeline.cpp:632-645
  D3  non-streaming chat, client reset while waiting    proxy_chat.cpp:505-515
  D4  embeddings, client reset while waiting            proxy_embeddings.cpp:166-172

Every case asserts the same accounting contract: exactly one request_log row
with status 499, zero tokens, zero equivalent_cost and zero billed_usage_cost
(the fixture prices the model, so pricing_status='rated' proves the zero is a
*priced* zero and not an unpriced row), exactly one request_attempts row, and
exactly one downstream/client_disconnect log line.

D3/D4 reset the client socket (RST) so the disconnect is a hard socket error,
the shape the non-streaming exits can observe deterministically: the proxy's
client-liveness checks only treat POLLHUP/POLLERR/POLLNVAL as gone.

Non-streaming clean FIN is a KNOWN LIMITATION, not a supported signal.  On TCP
a peer that close()s cannot be told apart from one that shutdown(SHUT_WR)s and
keeps reading, and audit 04 §3.7 states that judgement is intentional for
half-closed clients; treating the FIN as a disconnect would therefore invent
499s for clients that are still reading.  So a clean FIN is not asserted here
at all (the proxy keeps waiting and may record a response it could not
deliver); the clean-FIN contract is covered where it is safely observable —
the streaming case in the C-16 section of concurrency_regression_test.py.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
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

# Zero usage is only meaningful when the row was actually priced, so the
# fixture installs a CNY rate for every model.  11 prompt / 7 completion
# tokens at these prices would cost 7.8e-05, so a "zero" that carries tokens
# cannot be mistaken for a priced zero.
PRICING_RULE = (
    "INSERT INTO pricing_rules"
    "(id,model_pattern,priority,input_price,cache_read_price,output_price,"
    "currency) VALUES(1,'*',0,2.0,1.0,8.0,'CNY')"
)

# Settle delay: long enough for the proxy to read and route the request, short
# enough to stay well inside the mocked upstream delay.
SETTLE_SECS = 0.5
UPSTREAM_DELAY_SECS = 1.5


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


def open_request(port: int, key: str, path: str, body: dict) -> socket.socket:
    """Send one request on a raw socket and keep the connection open."""
    client = socket.create_connection(("127.0.0.1", port), timeout=8)
    client.sendall(
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {key}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    )
    return client


def reset_socket(client: socket.socket) -> None:
    """Close with RST so the peer sees POLLERR/POLLHUP (a hard error)."""
    client.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    client.close()


def send_complete_request(port: int, key: str, path: str, body: dict) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()
        return response.status


def wait_for_row(db: Path, model: str) -> tuple[list[dict], int]:
    """(all request_log rows for `model`, request_attempts row count)."""
    deadline = time.monotonic() + 8
    observed: list[dict] = []
    while time.monotonic() < deadline:
        with sqlite_connection(db) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,status_code,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,equivalent_cost,"
                "billed_usage_cost,pricing_status,is_streaming,attempt_count "
                "FROM request_log WHERE model=? ORDER BY id", (model,)
            ).fetchall()
            observed = [dict(item) for item in rows]
            if observed:
                attempts = conn.execute(
                    "SELECT count(*) FROM request_attempts a "
                    "JOIN request_log r ON r.id=a.request_log_id "
                    "WHERE r.model=?", (model,)
                ).fetchone()[0]
                return observed, attempts
        time.sleep(0.05)
    raise AssertionError(f"{model}: request_log row never appeared ({observed})")


def assert_zero_cost_499(db: Path, model: str, *,
                         streaming: bool) -> dict:
    """The 499 contract: one priced zero-cost row, one attempt, nothing else."""
    rows, attempts = wait_for_row(db, model)
    observed = (len(rows), attempts)
    assert observed == (1, 1), (model, observed, rows)
    row = rows[0]
    fields = (
        row["status_code"], row["prompt_tokens"], row["completion_tokens"],
        row["cache_read_tokens"], row["total_tokens"], row["equivalent_cost"],
        row["billed_usage_cost"], row["pricing_status"], row["is_streaming"],
        row["attempt_count"],
    )
    assert fields == (499, 0, 0, 0, 0, 0.0, 0.0, "rated", int(streaming), 1), \
        (model, fields)
    return row


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
    stderr = ""
    models: list[tuple[str, bool]] = []
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
                conn.execute(PRICING_RULE)
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

            # ── D1: committed stream, the downstream write fails ──────────
            body = json.dumps({
                "model": "client-disconnect-stream",
                "stream": True,
                "mock_simple_stream": True,
                "mock_long_stream_frames": 80,
                "mock_chunk_delay": 0.03,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            client = open_request(proxy_port, "tb-disconnect",
                                  "/v1/chat/completions", body)
            received = b""
            while b"data:" not in received:
                chunk = client.recv(4096)
                if not chunk:
                    raise AssertionError("proxy closed stream before first event")
                received += chunk
            # RST the downstream connection so the server must observe the
            # failed sink write while the upstream is still streaming.
            reset_socket(client)
            assert_zero_cost_499(db, "client-disconnect-stream", streaming=True)
            models.append(("client-disconnect-stream", True))

            follow_up = send_complete_request(
                proxy_port, "tb-disconnect", "/v1/chat/completions", {
                    "model": "after-disconnect",
                    "stream": False,
                    "messages": [{"role": "user", "content": "still alive"}],
                })
            assert follow_up == 200, follow_up

            # ── D2: stream cancelled before the first upstream byte ──────
            # No sink write can fail here, so the 499 can only come from the
            # terminal-phase disconnect check.
            body = json.dumps({
                "model": "client-disconnect-precommit",
                "stream": True,
                "mock_delay": UPSTREAM_DELAY_SECS,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            client = open_request(proxy_port, "tb-disconnect",
                                  "/v1/chat/completions", body)
            time.sleep(SETTLE_SECS)
            reset_socket(client)
            assert_zero_cost_499(db, "client-disconnect-precommit",
                                 streaming=True)
            models.append(("client-disconnect-precommit", True))

            # ── D3: non-streaming chat, client reset while waiting ───────
            body = json.dumps({
                "model": "client-disconnect-chat",
                "stream": False,
                "mock_delay": UPSTREAM_DELAY_SECS,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            client = open_request(proxy_port, "tb-disconnect",
                                  "/v1/chat/completions", body)
            time.sleep(SETTLE_SECS)
            # RST, not a clean FIN: see the known-limitation note above.
            reset_socket(client)
            assert_zero_cost_499(db, "client-disconnect-chat", streaming=False)
            models.append(("client-disconnect-chat", False))

            # ── D4: embeddings, client reset while waiting ───────────────
            body = json.dumps({
                "model": "client-disconnect-embeddings",
                "mock_delay": UPSTREAM_DELAY_SECS,
                "input": "hello",
            }).encode()
            client = open_request(proxy_port, "tb-disconnect",
                                  "/v1/embeddings", body)
            time.sleep(SETTLE_SECS)
            # RST, not a clean FIN: see the known-limitation note above.
            reset_socket(client)
            assert_zero_cost_499(db, "client-disconnect-embeddings",
                                 streaming=False)
            models.append(("client-disconnect-embeddings", False))

            # Stop the proxy while the fixture database still exists: the log
            # lines and the final row count are only meaningful after its writer
            # flushed, and the temporary directory holding the database is
            # removed as soon as this block exits.
            stderr = stop_process(proxy)
            proxy = None

            # The log is the third observable of a 499.  A single aborted request emits
            # two records: the failed upstream attempt and the downstream disconnect
            # written by log_proxy_client_disconnect.  The contract worth pinning is the
            # latter — exactly one downstream record per aborted request.
            lines = stderr.splitlines()
            downstream = [line for line in lines
                          if "reason=client_disconnect" in line
                          and "phase=downstream" in line]
            assert len(downstream) == len(models), (len(downstream), len(models),
                                                   stderr[-2000:])
            assert stderr.count("reason=client_disconnect") >= len(models), \
                stderr[-2000:]
            assert stderr.count("status=499") >= len(models), stderr[-2000:]
            # The writer flushed on shutdown: no case may have grown a second
            # accounting row after the first assertion.
            with sqlite_connection(db) as conn:
                for model, _ in models:
                    rows = conn.execute(
                        "SELECT count(*) FROM request_log WHERE model=?",
                        (model,)).fetchone()[0]
                    assert rows == 1, (model, rows)
            print("client disconnect recovery passed")
    finally:
        if proxy is not None:
            stderr = stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)



if __name__ == "__main__":
    main()
