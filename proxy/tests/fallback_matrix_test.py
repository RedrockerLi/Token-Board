#!/usr/bin/env python3
"""Fallback-matrix experiments (A3/A4/B1/B2/B3) against mock_upstream.

Drives the real proxy with an aggregate topology (plan account with two keys
+ Deepseek-API fallback) and asserts the request_attempts chains:

  A3  no fallback account: all plan keys cooled → fast 429 with 0 attempts.
  A4  fallback also transiently backed off → 0-attempt 429, recovers after
      the backoff window.
  B1  non-streaming 5xx fast-fail → [plan/500, plan/500, api/200].
  B2  streaming 429 pre-commit → buffered, not written to the client →
      [plan/429, plan/429, api/200].
  B3  streaming commit-then-stall (zen/go "吐首token后停滞") → semantic idle
      504 after the configured idle timeout; because content was already
      committed, NO fallback to the Deepseek-API key (attempt_count=1).
      This is review #5's baseline — measured with a short idle timeout here.

Usage:
  python3 fallback_matrix_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

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


def build_scenario(conn, local_key, model, plan_keys, deepseek_key=None):
    """plan account (plan_keys) + optional deepseek account, behind aggregate."""
    plan_id = conn.execute(
        "INSERT INTO upstream_accounts "
        "(name,upstream_key,base_url,api_format,auth_header,max_concurrency,"
        "account_type) VALUES (?,?,?,?,?,?,?)",
        (f"{local_key}-plan", "", "", "openai", "bearer", 64, "plan"),
    ).lastrowid
    conn.executemany(
        "INSERT INTO upstream_keys(account_id,key_value,position) VALUES (?,?,?)",
        [(plan_id, k, i) for i, k in enumerate(plan_keys)],
    )
    ds_id = None
    if deepseek_key:
        ds_id = conn.execute(
            "INSERT INTO upstream_accounts "
            "(name,upstream_key,base_url,api_format,auth_header,max_concurrency,"
            "account_type) VALUES (?,?,?,?,?,?,?)",
            (f"{local_key}-ds", "", "", "openai", "bearer", 64, "api"),
        ).lastrowid
        conn.execute(
            "INSERT INTO upstream_keys(account_id,key_value,position) VALUES (?,?,?)",
            (ds_id, deepseek_key, 0),
        )
    agg_id = conn.execute(
        "INSERT INTO upstream_accounts "
        "(name,upstream_key,base_url,api_format,auth_header,max_concurrency,"
        "account_type,is_aggregate) VALUES (?,?,?,?,?,?,?,?)",
        (f"{local_key}-agg", "", "", "openai", "bearer", 64, "api", 1),
    ).lastrowid
    entries = [(agg_id, 0, model, plan_id, model)]
    if ds_id:
        entries.append((agg_id, 1, model, ds_id, model))
    conn.executemany(
        "INSERT INTO aggregate_entries "
        "(account_id,sort_order,pattern,upstream_account_id,upstream_model) "
        "VALUES (?,?,?,?,?)",
        entries,
    )
    conn.execute(
        "INSERT INTO local_keys(key_value,label,account_id) VALUES (?,?,?)",
        (local_key, local_key, agg_id),
    )


def send(proxy_port, local_key, body, timeout=30):
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code
    except Exception as e:  # noqa: BLE001 — e.g. truncated stream (B3)
        return 0


def latest_attempts(db_path, model, min_id=0):
    conn = sqlite3.connect(db_path)
    try:
        for _ in range(120):
            row = conn.execute(
                "SELECT id, attempt_count, status_code FROM request_log "
                "WHERE model=? AND id>? ORDER BY id DESC LIMIT 1",
                (model, min_id),
            ).fetchone()
            if row:
                attempts = conn.execute(
                    "SELECT a.status_code FROM request_attempts a "
                    "WHERE a.request_log_id=? ORDER BY a.attempt_index",
                    (row[0],),
                ).fetchall()
                return (row[0], row[1], [a[0] for a in attempts])
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
    from app.db.migrations import migrate
    from scripts.mock_upstream import Handler, ThreadingHTTPServer

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "proxy.db"
        legacy = Path(tmp) / "schema"
        legacy.mkdir()
        for mig in schema_dir.glob("*.sql"):
            if int(mig.stem.split("_", 1)[0].split("-", 1)[1]) <= 10:
                shutil.copy2(mig, legacy / mig.name)
        migrate(str(db_path), str(legacy))
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            build_scenario(conn, "tb-a3", "a3-model", ["sk-a3-p1", "sk-a3-p2"])
            build_scenario(conn, "tb-a4", "a4-model", ["sk-a4-p1", "sk-a4-p2"], "sk-a4-d")
            build_scenario(conn, "tb-b1", "b1-model", ["sk-b1-p1", "sk-b1-p2"], "sk-b1-d")
            build_scenario(conn, "tb-b2", "b2-model", ["sk-b2-p1", "sk-b2-p2"], "sk-b2-d")
            build_scenario(conn, "tb-b3", "b3-model", ["sk-b3-p1"], "sk-b3-d")
            conn.execute("UPDATE upstream_accounts SET base_url=?", (base_url,))
            # Fast B3 baseline: shrink the OpenAI-format semantic idle timeout
            # so the commit-then-stall → 504 round-trip completes in seconds.
            conn.execute(
                "UPDATE proxy_timeout_config SET "
                "streaming_first_byte_timeout=10, streaming_idle_timeout=4 "
                "WHERE app_type='openai'")
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_for_health(proxy_port)
            common = {"messages": [{"role": "user", "content": "hi"}]}

            # ── A3: no fallback; both plan keys GoUsageLimit → fast 429 ──
            send(proxy_port, "tb-a3", {**common, "model": "a3-model", "stream": False,
                "mock_status_by_key": {"sk-a3-p1": 429, "sk-a3-p2": 429},
                "mock_error_type_by_key": {"sk-a3-p1": "GoUsageLimitError",
                                           "sk-a3-p2": "GoUsageLimitError"}})
            rid, n, st = latest_attempts(db_path, "a3-model")
            assert st == [429, 429], f"A3 req1 chain {st}"
            send(proxy_port, "tb-a3", {**common, "model": "a3-model", "stream": False})
            _, n2, st2 = latest_attempts(db_path, "a3-model", rid)
            assert n2 == 0 and st2 == [], f"A3 req2 must be 0-attempt fast 429: {st2}"
            print(f"A3 OK: all plan cooled, no fallback -> 0-attempt 429 (chain1={st})")

            # ── A4: plan cooled + fallback transiently backed off ──
            send(proxy_port, "tb-a4", {**common, "model": "a4-model", "stream": False,
                "mock_status_by_key": {"sk-a4-p1": 429, "sk-a4-p2": 429, "sk-a4-d": 502},
                "mock_error_type_by_key": {"sk-a4-p1": "GoUsageLimitError",
                                           "sk-a4-p2": "GoUsageLimitError"}})
            rid, _, st = latest_attempts(db_path, "a4-model")
            assert st == [429, 429, 502], f"A4 req1 chain {st}"
            send(proxy_port, "tb-a4", {**common, "model": "a4-model", "stream": False})
            _, n2, st2 = latest_attempts(db_path, "a4-model", rid)
            assert n2 == 0 and st2 == [], f"A4 req2 must be 0-attempt 429: {st2}"
            time.sleep(6.0)  # api key's 5s transient backoff expires
            send(proxy_port, "tb-a4", {**common, "model": "a4-model", "stream": False})
            _, n3, st3 = latest_attempts(db_path, "a4-model", rid + 1)
            assert st3 == [200], f"A4 req3 fallback must recover: {st3}"
            print(f"A4 OK: fallback backed off -> 0-attempt 429, recovered "
                  f"after 6s (chain1={st}, chain3={st3})")

            # ── B1: non-streaming 5xx fast-fail → next candidates ──
            send(proxy_port, "tb-b1", {**common, "model": "b1-model", "stream": False,
                "mock_status_by_key": {"sk-b1-p1": 500, "sk-b1-p2": 500, "sk-b1-d": 200}})
            rid, _, st = latest_attempts(db_path, "b1-model")
            assert st == [500, 500, 200], f"B1 chain {st}"
            print(f"B1 OK: non-streaming 5xx fast-fail falls through ({st})")

            # ── B2: streaming 429 pre-commit → buffered, fall back ──
            send(proxy_port, "tb-b2", {**common, "model": "b2-model", "stream": True,
                "mock_status_by_key": {"sk-b2-p1": 429, "sk-b2-p2": 429, "sk-b2-d": 200}})
            rid, _, st = latest_attempts(db_path, "b2-model")
            assert st == [429, 429, 200], f"B2 chain {st}"
            print(f"B2 OK: streaming 429 pre-commit buffered -> fallback ({st})")

            # ── B3: commit-then-stall → semantic idle 504, NO fallback (#5) ──
            # mock_stall_after=2 commits a SEMANTIC frame (role-start +
            # reasoning delta) before the stall — the true "吐首token后停滞"
            # shape.  (stall_after=1 would stall before any semantic event.)
            start = time.monotonic()
            send(proxy_port, "tb-b3", {**common, "model": "b3-model", "stream": True,
                "mock_stall_after": 2, "mock_stall_secs": 8.0}, timeout=20)
            rid, n, st = latest_attempts(db_path, "b3-model")
            elapsed = time.monotonic() - start
            assert st == [504] and n == 1, f"B3 must be single-attempt 504: {st}"
            assert elapsed < 8.5, f"B3 must 504 at the 4s idle timeout, took {elapsed:.1f}s"
            print(f"B3 OK (#5 baseline): commit-then-stall -> 504 after "
                  f"{elapsed:.1f}s, attempt_count=1, NO fallback to deepseek ({st})")

            print("fallback matrix tests passed")
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
