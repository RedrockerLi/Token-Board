#!/usr/bin/env python3
"""GoUsageLimitError cooldown discrimination — end-to-end (#4 root B).

Drives the real proxy against mock_upstream with an aggregate topology
(plan account with two keys + Deepseek-API fallback, behind one aggregate
root), and asserts the request_attempts chain for each scenario:

  A1  GoUsageLimitError 429 on plan key1 → key1 cools down (5h); same-request
      falls to key2, and a subsequent request skips key1 entirely.
  A1b plain 429 (no GoUsageLimitError) on a single key → the next request
      immediately retries that key (proves there is no cross-request backoff).
  A2  both plan keys GoUsageLimitError → both cool down; requests fall to the
      Deepseek-API fallback and later skip both plan keys.

Usage:
  python3 cooldown_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import os

import argparse
import json
import shutil
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


def build_scenario(conn, local_key, model, plan_keys, deepseek_key):
    """plan account (plan_keys) + optional deepseek account, behind aggregate."""
    from v1_fixture import build_aggregate_scenario
    return build_aggregate_scenario(
        conn, local_key, model, plan_keys, deepseek_key)


def send(proxy_port, local_key, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def latest_attempts(db_path, model, min_id=0):
    """Poll for the newest request_log row of `model` with id > min_id.

    Returns (row_id, attempt_count, [statuses], [upstream_key_ids]); None if
    no newer row appears within the deadline.
    """
    conn = sqlite3.connect(db_path)
    try:
        for _ in range(100):
            row = conn.execute(
                "SELECT id, attempt_count, status_code FROM request_log "
                "WHERE model=? AND id>? ORDER BY id DESC LIMIT 1",
                (model, min_id),
            ).fetchone()
            if row:
                attempts = conn.execute(
                    "SELECT a.status_code, a.upstream_key_id, a.account_id "
                    "FROM request_attempts a WHERE a.request_log_id=? "
                    "ORDER BY a.attempt_index", (row[0],)
                ).fetchall()
                return (row[0], row[1],
                        [a[0] for a in attempts], [a[1] for a in attempts])
            time.sleep(0.05)
        return None
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
    from scripts.mock_upstream import Handler, ThreadingHTTPServer
    from v1_fixture import ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            # back-fill base_url (accounts were inserted with "" then updated)
            build_scenario(conn, "tb-a1", "a1-model", ["sk-a1-p1", "sk-a1-p2"], "sk-a1-d")
            build_scenario(conn, "tb-a1b", "a1b-model", ["sk-a1b-p1"], None)
            build_scenario(conn, "tb-a2", "a2-model", ["sk-a2-p1", "sk-a2-p2"], "sk-a2-d")
            conn.execute("UPDATE upstreams SET base_url=?", (base_url,))
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=os.environ.copy())
        try:
            wait_for_health(proxy_port)
            common = {"stream": False,
                      "messages": [{"role": "user", "content": "hi"}]}

            # ── A1: GoUsageLimitError on key1 → cooldown, fall to key2 ──
            send(proxy_port, "tb-a1", {**common, "model": "a1-model",
                "mock_status_by_key": {"sk-a1-p1": 429, "sk-a1-p2": 200},
                "mock_error_type_by_key": {"sk-a1-p1": "GoUsageLimitError"}})
            rid, n1, st1, keys1 = latest_attempts(db_path, "a1-model")
            assert st1 == [429, 200], f"A1 req1 chain {st1}"
            assert keys1[0] is not None and keys1[1] is not None

            send(proxy_port, "tb-a1", {**common, "model": "a1-model"})
            _, n2, st2, _ = latest_attempts(db_path, "a1-model", rid)
            assert n2 == 1 and st2 == [200], f"A1 req2 must skip cooled key1: {st2}"
            print("A1 OK: GoUsageLimitError key1 -> cooldown, fallback key2, "
                  f"skip on next request (chain1={st1}, chain2={st2})")

            # ── A1b: plain 429 is not remembered across requests ──
            send(proxy_port, "tb-a1b", {**common, "model": "a1b-model",
                "mock_status_by_key": {"sk-a1b-p1": 429}})
            rid, n1, st1, _ = latest_attempts(db_path, "a1b-model")
            assert n1 == 1 and st1 == [429], f"A1b req1 {n1=} {st1}"

            send(proxy_port, "tb-a1b", {**common, "model": "a1b-model"})
            _, n2, st2, _ = latest_attempts(db_path, "a1b-model", rid)
            assert n2 == 1 and st2 == [200], f"A1b req2 must retry key1: {n2=} {st2}"
            print("A1b OK: plain 429 does not create cross-request key state "
                  f"(chain1={st1}, chain2={st2})")

            # ── A2: both plan keys GoUsageLimit → fall to Deepseek-API ──
            send(proxy_port, "tb-a2", {**common, "model": "a2-model",
                "mock_status_by_key": {"sk-a2-p1": 429, "sk-a2-p2": 429,
                                       "sk-a2-d": 200},
                "mock_error_type_by_key": {"sk-a2-p1": "GoUsageLimitError",
                                           "sk-a2-p2": "GoUsageLimitError"}})
            rid, n1, st1, _ = latest_attempts(db_path, "a2-model")
            assert st1 == [429, 429, 200], f"A2 req1 chain {st1}"

            send(proxy_port, "tb-a2", {**common, "model": "a2-model"})
            _, n2, st2, _ = latest_attempts(db_path, "a2-model", rid)
            assert n2 == 1 and st2 == [200], \
                f"A2 req2 must skip both cooled plan keys: {st2}"
            print("A2 OK: both plan keys cooled -> deepseek fallback, "
                  f"skipped next request (chain1={st1}, chain2={st2})")

            print("cooldown discrimination tests passed")
        finally:
            proxy.terminate()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
            if proxy.returncode not in (0, -15):
                print(proxy.stderr.read(), file=sys.stderr)

    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
