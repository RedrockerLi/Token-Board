#!/usr/bin/env python3
"""Cooldown-recovery experiment: a cooled plan key is re-checked every
interval and cleared early when the upstream reports healthy.

Drives the real proxy (with TB_COOLDOWN_PROBE_SECS=2) + mock_upstream.
Two probe shapes coexist:
  - a plan account WITHOUT a catalog model falls back to the free GET /models
    probe;
  - a plan account WITH a catalog model gets the minimal "你好" chat probe.
Both carry no request-body mock_* flags, so their per-key status is pinned
out-of-band via POST /__ctrl (which also drives bodyless POSTs).

Flow (run twice, once per probe shape):
  cool key1 (GoUsageLimitError)        → key1 skipped, key2 serves
  flip mock key1 → 200, wait for probe → key1 serves again (probe cleared it)
  re-cool key1, keep mock 429, wait    → key1 still skipped (probe saw 429)

Which key served is asserted via request_attempts.upstream_key_id.

Usage:
  python3 cooldown_probe_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
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


def build_scenario(conn, local_key, model, plan_keys, deepseek_key):
    """plan account (plan_keys) + deepseek fallback, behind aggregate."""
    from v1_fixture import build_aggregate_scenario
    return build_aggregate_scenario(
        conn, local_key, model, plan_keys, deepseek_key)


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


def ctrl(upstream_port, method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{upstream_port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def latest_attempts(db_path, model, min_id=0):
    conn = sqlite3.connect(db_path)
    try:
        for _ in range(160):
            row = conn.execute(
                "SELECT id, attempt_count, status_code FROM request_log "
                "WHERE model=? AND id>? ORDER BY id DESC LIMIT 1",
                (model, min_id),
            ).fetchone()
            if row:
                attempts = conn.execute(
                    "SELECT a.status_code, a.upstream_key_id FROM request_attempts a "
                    "WHERE a.request_log_id=? ORDER BY a.attempt_index",
                    (row[0],),
                ).fetchall()
                return (row[0], row[1], [a[0] for a in attempts],
                        [a[1] for a in attempts])
            time.sleep(0.05)
        return None
    finally:
        conn.close()


def wait_probe(upstream_port, key, after_count):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        status = ctrl(upstream_port, "GET", "/__ctrl/status")
        if status["probes"].get(key, 0) > after_count:
            return
        time.sleep(0.2)
    raise AssertionError(f"cooldown probe for {key} never fired")


def send_until_start(db_path, proxy_port, local_key, body, model, key_id,
                     min_id):
    """Send requests until one starts at *key_id*.

    Tied candidates rotate their start per request, so a scenario that must
    exercise a specific first key is reached by exercising both parities.
    Returns (rid, statuses, keys) of the matching request.
    """
    rid = min_id
    for _ in range(2):
        send(proxy_port, local_key, body)
        rid, _, statuses, keys = latest_attempts(db_path, model, rid)
        if keys and keys[0] == key_id:
            return rid, statuses, keys
    raise AssertionError(f"{model}: no request started at key {key_id}")


def chat_probe_count(log_path, key):
    """Minimal "你好" probe POSTs for *key* recorded in the mock log."""
    if not Path(log_path).exists():
        return 0
    count = 0
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if (record.get("method") == "POST"
                and record.get("auth") == f"Bearer {key}"
                and (record.get("body") or {}).get("messages")
                == [{"role": "user", "content": "你好"}]):
            count += 1
    return count


def wait_chat_probe(log_path, key, after_count):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if chat_probe_count(log_path, key) > after_count:
            return
        time.sleep(0.2)
    raise AssertionError(f"chat probe for {key} never fired")


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
    import scripts.mock_upstream as mock_upstream
    from scripts.mock_upstream import Handler, ThreadingHTTPServer
    from v1_fixture import ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "mock_upstream.log"
        mock_upstream.LOG_FILE = str(log_path)
        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            build_scenario(conn, "tb-p", "p-model", ["sk-p1", "sk-p2"], "sk-p-d")
            # tb-q carries a catalog model, so its probe is the minimal
            # "你好" chat instead of the GET /models fallback.
            _, q_key_ids, _ = build_scenario(
                conn, "tb-q", "q-model", ["sk-q1", "sk-q2"], "sk-q-d")
            q_upstream = conn.execute(
                "SELECT c.upstream_id FROM upstream_credentials c "
                "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                "WHERE s.secret_value='sk-q1'").fetchone()[0]
            conn.execute(
                "INSERT INTO upstream_model_catalog(upstream_id, model_id) "
                "VALUES(?,?)", (q_upstream, "q-probe-model"))
            conn.execute("UPDATE upstreams SET base_url=?", (base_url,))
            conn.commit()
            key_ids = {kv: k for k, kv in conn.execute(
                "SELECT c.runtime_id,s.secret_value FROM upstream_credentials c "
                "JOIN upstream_secrets s ON s.credential_uuid=c.uuid "
                "WHERE s.secret_value LIKE 'sk-p%'")}
        finally:
            conn.close()
        p1_id, p2_id = key_ids["sk-p1"], key_ids["sk-p2"]
        q1_id, q2_id = q_key_ids[0], q_key_ids[1]

        proxy_port = free_port()
        env = {**os.environ, "TB_COOLDOWN_PROBE_SECS": "2"}
        command = [str(proxy_binary), "--db", str(db_path), "--host",
                   "127.0.0.1", "--port", str(proxy_port)]
        if os.environ.get("TB_TEST_DEBUG"):
            command.extend(["--log-level", "debug"])
        proxy = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=None if os.environ.get("TB_TEST_DEBUG") else subprocess.PIPE,
            text=True,
            env=env)
        try:
            wait_for_health(proxy_port)
            common = {"stream": False,
                      "messages": [{"role": "user", "content": "hi"}]}

            # Pin the mock so any early probe sees 429 (keeps cooldown) until
            # the positive step flips it to 200.
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-p1": 429}})

            # ── cool key1: [p1/429 GoUsageLimit, p2/200] ──
            send(proxy_port, "tb-p", {**common, "model": "p-model",
                "mock_status_by_key": {"sk-p1": 429, "sk-p2": 200},
                "mock_error_type_by_key": {"sk-p1": "GoUsageLimitError"}})
            rid, n, st, keys = latest_attempts(db_path, "p-model")
            assert st == [429, 200], f"req1 chain {st}"
            assert keys[0] == p1_id and keys[1] == p2_id, f"req1 keys {keys}"

            # ── next request: key1 skipped, key2 serves ──
            send(proxy_port, "tb-p", {**common, "model": "p-model"})
            rid2, _, st2, keys2 = latest_attempts(db_path, "p-model", rid)
            assert st2 == [200] and keys2[0] == p2_id, \
                f"req2 must be served by p2 (key1 skipped): st={st2} keys={keys2}"
            print(f"OK: key1 cooled -> skipped (req2 served by key2 {p2_id})")

            # ── positive: flip mock p1 → 200; next probe clears cooldown ──
            before = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"].get("sk-p1", 0)
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-p1": 200}})
            wait_probe(upstream_port, "sk-p1", before)   # probe ran after flip
            send(proxy_port, "tb-p", {**common, "model": "p-model"})
            rid3, _, st3, keys3 = latest_attempts(db_path, "p-model", rid2)
            assert st3 == [200] and keys3[0] == p1_id, \
                f"req3 must be served by p1 (probe cleared it): st={st3} keys={keys3}"
            print(f"OK: probe cleared key1 early (req3 served by key1 {p1_id})")

            # ── negative: re-cool key1, keep mock 429; probe retains cooldown ──
            # Only the request that starts at key1 re-cools it, so send until
            # the rotation actually does.
            recool_body = {**common, "model": "p-model",
                           "mock_status_by_key": {"sk-p1": 429, "sk-p2": 200},
                           "mock_error_type_by_key":
                               {"sk-p1": "GoUsageLimitError"}}
            rid4, st4, keys4 = send_until_start(
                db_path, proxy_port, "tb-p", recool_body, "p-model", p1_id,
                rid3)
            assert st4 == [429, 200], f"req4 recool chain {st4} {keys4}"
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-p1": 429}})
            before = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"].get("sk-p1", 0)
            wait_probe(upstream_port, "sk-p1", before)   # probe saw 429 again
            # Both start parities: key1 must be skipped (never attempted) and
            # key2 must keep serving.
            for _ in range(2):
                send(proxy_port, "tb-p", {**common, "model": "p-model"})
                rid4, _, st5, keys5 = latest_attempts(db_path, "p-model", rid4)
                assert st5 == [200] and keys5[0] == p2_id, \
                    f"req5 must be served by p2 (probe saw 429): st={st5} keys={keys5}"
                assert p1_id not in keys5, \
                    f"re-cooled key1 must never be attempted: {keys5}"
            print(f"OK: probe kept key1 cooled (requests served by key2 {p2_id})")

            # ── chat-probe flow: tb-q's account carries a catalog model, so
            #    its recovery probe is a minimal "你好" chat per interval ──
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-q1": 429}})
            q_body = {**common, "model": "q-model",
                      "mock_status_by_key": {"sk-q1": 429, "sk-q2": 200},
                      "mock_error_type_by_key": {"sk-q1": "GoUsageLimitError"}}
            qrid, qst, qkeys = send_until_start(
                db_path, proxy_port, "tb-q", q_body, "q-model", q1_id, 0)
            assert qst == [429, 200] and qkeys[1] == q2_id, \
                f"chat probe req1 chain {qst} {qkeys}"
            wait_chat_probe(log_path, "sk-q1", 0)  # pinned 429 keeps cooling
            for _ in range(2):
                send(proxy_port, "tb-q", {**common, "model": "q-model"})
                qrid, _, qst, qkeys = latest_attempts(db_path, "q-model", qrid)
                assert qst == [200] and qkeys[0] == q2_id, \
                    f"q1 stays cooling while the chat probe is pinned " \
                    f"(q2 serves): {qst} {qkeys}"
                assert q1_id not in qkeys, \
                    f"cooling q1 must never be attempted: {qkeys}"
            print(f"OK: chat probe saw 429, q1 stayed cooling (q2 {q2_id})")

            before = chat_probe_count(log_path, "sk-q1")
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-q1": 200}})
            wait_chat_probe(log_path, "sk-q1", before)  # probe ran after flip
            time.sleep(0.5)  # let the cleared cooldown land before asking again
            qrid, qst, qkeys = send_until_start(
                db_path, proxy_port, "tb-q", {**common, "model": "q-model"},
                "q-model", q1_id, qrid)
            assert qst == [200], f"chat probe must clear q1: {qst} {qkeys}"
            print(f"OK: chat probe cleared q1 ({qst} {qkeys})")

            print("cooldown probe test passed")
        finally:
            diagnostics = stop_process(proxy, check_returncode=False)
            if proxy.returncode not in (0, -15):
                print(diagnostics, file=sys.stderr)

    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
