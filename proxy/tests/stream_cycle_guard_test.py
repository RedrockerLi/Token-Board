#!/usr/bin/env python3
"""End-to-end: an upstream response that carries two complete message cycles.

The mock appends a second, complete cycle (different id, inflated usage) to the
first one — the shape observed from opencode.ai Zen/Go.  The proxy must deliver
exactly one cycle to the client and record the first cycle's usage.

Cases:
  G1  anthropic passthrough (/v1/messages)     body flag
  G2  openai passthrough (/v1/chat/completions) body flag
  G3  responses passthrough (/v1/responses)     body flag
  G4  converted (openai client, anthropic upstream) via model-name trigger

Usage:
  python3 stream_cycle_guard_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
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
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                        timeout=0.2) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def post(port: int, key: str, path: str, body: dict,
         timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "x-api-key": key,
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def latest_usage(db_path: Path, model: str) -> tuple[int, int]:
    """(completion_tokens, attempt_count) of the newest row for `model`."""
    conn = sqlite3.connect(db_path)
    try:
        for _ in range(120):
            row = conn.execute(
                "SELECT completion_tokens, attempt_count FROM request_log "
                "WHERE model=? ORDER BY id DESC LIMIT 1", (model,)).fetchone()
            if row:
                return row[0], row[1]
            time.sleep(0.05)
        raise AssertionError(f"no request_log row for {model}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token_proxy", type=str)
    ap.add_argument("schema_dir", type=str)
    ap.add_argument("project_root", type=str)
    args = ap.parse_args()

    proxy_binary = Path(args.token_proxy).resolve()
    schema_dir = Path(args.schema_dir).resolve()
    project_root = Path(args.project_root).resolve()
    sys.path.insert(0, str(project_root))
    from scripts.mock_upstream import Handler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        try:
            for local_key, api_format in (
                ("tb-g1", "anthropic"),
                ("tb-g2", "openai"),
                ("tb-g3", "openai_responses"),
                ("tb-g4", "anthropic"),
            ):
                account, target, _ = add_upstream(
                    conn, local_key, "", [f"sk-{local_key}-p1"],
                    api_format=api_format)
                add_plain_route(conn, local_key, f"{local_key}-route",
                                target, account)
            conn.execute("UPDATE upstreams SET base_url=?",
                         (f"http://127.0.0.1:{upstream_port}",))
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--host", "127.0.0.1",
             "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env={**os.environ})
        try:
            wait_for_health(proxy_port)
            common = {"stream": True,
                      "mock_duplicate_cycle": True,
                      "messages": [{"role": "user", "content": "hi"}]}

            # G1: Anthropic passthrough.
            status, text = post(proxy_port, "tb-g1", "/v1/messages",
                                {**common, "model": "g1-model", "max_tokens": 64})
            assert status == 200, f"G1 status {status}: {text[:200]}"
            assert text.count("event: message_start") == 1, \
                f"G1 duplicate message_start: {text.count('event: message_start')}"
            assert text.count("event: message_stop") == 1, "G1 duplicate terminal"
            assert text.count('"text":"Hel"') == 1, "G1 second cycle forwarded"
            completion, attempts = latest_usage(db_path, "g1-model")
            assert completion == 7, f"G1 usage from second cycle: {completion}"
            assert attempts == 1, f"G1 attempts {attempts}"

            # G2: OpenAI passthrough on the simple (fixed usage) stream.
            status, text = post(proxy_port, "tb-g2", "/v1/chat/completions",
                                {**common, "model": "g2-model",
                                 "mock_simple_stream": True})
            assert status == 200, f"G2 status {status}: {text[:200]}"
            assert text.count("data: [DONE]") == 1, "G2 duplicate [DONE]"
            assert text.count('"content": "Hel"') == 1, "G2 second cycle forwarded"
            completion, _ = latest_usage(db_path, "g2-model")
            assert completion == 7, f"G2 usage from second cycle: {completion}"

            # G3: Responses passthrough.
            status, text = post(proxy_port, "tb-g3", "/v1/responses",
                                {**common, "model": "g3-model",
                                 "input": "hi", "max_output_tokens": 64})
            assert status == 200, f"G3 status {status}: {text[:200]}"
            assert text.count('"type":"response.created"') == 1, \
                "G3 duplicate response.created"
            assert text.count('"type":"response.completed"') == 1, \
                "G3 duplicate terminal"
            completion, _ = latest_usage(db_path, "g3-model")
            assert completion == 7, f"G3 usage from second cycle: {completion}"

            # G4: converted route — openai client, anthropic upstream.  The IR
            # conversion drops the body flag, so the model name triggers it.
            status, text = post(proxy_port, "tb-g4", "/v1/chat/completions",
                                {"model": "mock-dup-cycle",
                                 "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]})
            assert status == 200, f"G4 status {status}: {text[:200]}"
            assert text.count("data: [DONE]") == 1, "G4 duplicate [DONE]"
            assert text.count('"content":"Hel"') + \
                text.count('"content": "Hel"') == 1, \
                "G4 second cycle forwarded"
            completion, _ = latest_usage(db_path, "mock-dup-cycle")
            assert completion == 7, f"G4 usage from second cycle: {completion}"

            print("G1-G4 OK: exactly one message cycle per upstream response")
        finally:
            stderr = stop_process(proxy)
            upstream.shutdown()
        assert "upstream_duplicate_cycle" in stderr, \
            "proxy did not log the duplicate cycle"


if __name__ == "__main__":
    main()
