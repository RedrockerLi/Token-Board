#!/usr/bin/env python3
"""Pin the two production gray switches in their OFF position.

Both switches are documented as production knobs (doc/development.md:63) and
both default to ON, so the ON path is what every other test exercises.  A
switch whose OFF branch never runs is a semantic surface nobody protects; each
case here runs the same request twice — once with the switch defaulted on
(control) and once with it set to 0 — so the assertion is "the switch changes
this behaviour", not "the response happens to look right".

  S1 PROXY_STRICT_TERMINAL=0   upstream 2xx stream that ends without a terminal
                               frame: control 502 (truncated), off 200 with the
                               usage the frames carried.
  S2 PROXY_STREAM_CYCLE_GUARD=0  upstream splices a second complete generation
                               into the same response: control forwards one
                               cycle, off forwards both verbatim.

Each proxy exits before the next one starts: the durable request-log spool is
flock-owned by a single process, so overlapping proxies would mask a real
failure behind "spool is already owned".
"""

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
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from support import stop_process

TRUNCATION_MARKER = "Upstream stream truncated before terminal event"

# The mock's default OpenAI stream carries 11 prompt / 7 completion tokens.
EXPECTED_TOKENS = (11, 7, 0, 18)  # prompt, completion, cache_read, total

CASES = ("strict-control", "guard-control", "strict-off", "guard-off")


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


@contextmanager
def running_proxy(binary: Path, db: Path,
                  extra_env: dict | None) -> Iterator[int]:
    port = free_port()
    env = os.environ.copy()
    env.update(extra_env or {})
    process = subprocess.Popen(
        [str(binary), "--db", str(db), "--host", "127.0.0.1",
         "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        wait_for_health(port)
        yield port
    finally:
        stop_process(process)


def post(port: int, key: str, body: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def wait_for_log(db: Path, model: str) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status_code,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,attempt_count "
                "FROM request_log WHERE model=? ORDER BY id DESC LIMIT 1",
                (model,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            return dict(row)
        time.sleep(0.05)
    raise AssertionError(f"{model}: request_log row never appeared")


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
        with tempfile.TemporaryDirectory(prefix="token-board-gray-switch-") as raw:
            db = Path(raw) / "token-board.db"
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                # One account per case: a failed stream parks its key in the
                # failure-avoid window, which would otherwise turn the next
                # case into a 503 instead of the behaviour under test.
                for tag in CASES:
                    account, upstream_id, _ = add_upstream(
                        conn, tag, f"http://127.0.0.1:{upstream_port}",
                        [f"sk-{tag}"], max_concurrency=4)
                    add_plain_route(conn, f"tb-{tag}", f"{tag}-route",
                                    upstream_id, account)
                conn.commit()
            finally:
                conn.close()

            # The truncated-stream request: every frame arrives, no terminal.
            truncated = {
                "stream": True,
                "mock_omit_finish_reason": True,
                "mock_omit_terminal": True,
                "messages": [{"role": "user", "content": "hi"}],
            }
            # The spliced-generation request: two complete message cycles.
            duplicated = {
                "stream": True,
                "mock_simple_stream": True,
                "mock_duplicate_cycle": True,
                "messages": [{"role": "user", "content": "hi"}],
            }

            # ── Controls: default configuration (both switches ON) ───────
            with running_proxy(binary, db, None) as port:
                status, body = post(port, "tb-strict-control",
                                    {**truncated, "model": "strict-control"})
                assert status == 200, (status, body[:200])
                control = wait_for_log(db, "strict-control")
                assert (control["status_code"], control["prompt_tokens"],
                        control["completion_tokens"],
                        control["total_tokens"]) == (502, 0, 0, 0), control
                assert TRUNCATION_MARKER in body, body[-300:]

                status, body = post(port, "tb-guard-control",
                                    {**duplicated, "model": "guard-control"})
                assert status == 200, (status, body[:200])
                assert body.count("data: [DONE]") == 1, \
                    body.count("data: [DONE]")
                assert wait_for_log(db, "guard-control")["status_code"] == 200

            # ── S1: PROXY_STRICT_TERMINAL=0 ─────────────────────────────
            with running_proxy(binary, db,
                               {"PROXY_STRICT_TERMINAL": "0"}) as port:
                status, body = post(port, "tb-strict-off",
                                    {**truncated, "model": "strict-off"})
                assert status == 200, (status, body[:200])
                assert TRUNCATION_MARKER not in body, body[-300:]
                row = wait_for_log(db, "strict-off")
                observed = (row["status_code"], row["prompt_tokens"],
                            row["completion_tokens"],
                            row["cache_read_tokens"], row["total_tokens"],
                            row["attempt_count"])
                assert observed == (200, *EXPECTED_TOKENS, 1), observed

            # ── S2: PROXY_STREAM_CYCLE_GUARD=0 ─────────────────────────
            with running_proxy(binary, db,
                               {"PROXY_STREAM_CYCLE_GUARD": "0"}) as port:
                status, body = post(port, "tb-guard-off",
                                    {**duplicated, "model": "guard-off"})
                assert status == 200, (status, body[:200])
                # Verbatim passthrough: both generations reach the client.
                assert body.count("data: [DONE]") == 2, \
                    body.count("data: [DONE]")
                assert body.count('"content": "Hel"') == 2, \
                    body.count('"content": "Hel"')
                assert wait_for_log(db, "guard-off")["status_code"] == 200
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("gray switch closed loops passed")


if __name__ == "__main__":
    main()
