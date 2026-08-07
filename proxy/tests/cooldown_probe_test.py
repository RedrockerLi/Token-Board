#!/usr/bin/env python3
"""Cooldown-probe experiment: a cooled plan key is re-probed every interval
and cleared early when the upstream reports healthy.

Drives the real proxy (with TB_COOLDOWN_PROBE_SECS=2) + mock_upstream.
The mock's per-key GET status is toggled out-of-band via POST /__ctrl because
the probe is a bare GET /models that carries no request-body mock_* flags.

Flow:
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
    conn.executemany(
        "INSERT INTO aggregate_entries "
        "(account_id,sort_order,pattern,upstream_account_id,upstream_model) "
        "VALUES (?,?,?,?,?)",
        [(agg_id, 0, model, plan_id, model), (agg_id, 1, model, ds_id, model)],
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
            if int(mig.stem.split("_", 1)[0]) <= 10:
                shutil.copy2(mig, legacy / mig.name)
        migrate(str(db_path), str(legacy))
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            build_scenario(conn, "tb-p", "p-model", ["sk-p1", "sk-p2"], "sk-p-d")
            conn.execute("UPDATE upstream_accounts SET base_url=?", (base_url,))
            conn.commit()
            key_ids = {kv: k for k, kv in conn.execute(
                "SELECT id, key_value FROM upstream_keys WHERE key_value LIKE 'sk-p%'")}
        finally:
            conn.close()
        p1_id, p2_id = key_ids["sk-p1"], key_ids["sk-p2"]

        proxy_port = free_port()
        env = {**os.environ, "TB_COOLDOWN_PROBE_SECS": "2"}
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
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
            _, _, st2, keys2 = latest_attempts(db_path, "p-model", rid)
            assert st2 == [200] and keys2[0] == p2_id, \
                f"req2 must be served by p2 (key1 skipped): st={st2} keys={keys2}"
            print(f"OK: key1 cooled -> skipped (req2 served by key2 {p2_id})")

            # ── positive: flip mock p1 → 200; next probe clears cooldown ──
            before = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"].get("sk-p1", 0)
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-p1": 200}})
            wait_probe(upstream_port, "sk-p1", before)   # probe ran after flip
            send(proxy_port, "tb-p", {**common, "model": "p-model"})
            _, _, st3, keys3 = latest_attempts(db_path, "p-model", rid)
            assert st3 == [200] and keys3[0] == p1_id, \
                f"req3 must be served by p1 (probe cleared it): st={st3} keys={keys3}"
            print(f"OK: probe cleared key1 early (req3 served by key1 {p1_id})")

            # ── negative: re-cool key1, keep mock 429; probe retains cooldown ──
            send(proxy_port, "tb-p", {**common, "model": "p-model",
                "mock_status_by_key": {"sk-p1": 429, "sk-p2": 200},
                "mock_error_type_by_key": {"sk-p1": "GoUsageLimitError"}})
            rid4, _, st4, _ = latest_attempts(db_path, "p-model", rid)
            assert st4 == [429, 200], f"req4 recool chain {st4}"
            ctrl(upstream_port, "POST", "/__ctrl", {"set": {"sk-p1": 429}})
            before = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"].get("sk-p1", 0)
            wait_probe(upstream_port, "sk-p1", before)   # probe saw 429 again
            send(proxy_port, "tb-p", {**common, "model": "p-model"})
            _, _, st5, keys5 = latest_attempts(db_path, "p-model", rid4)
            assert st5 == [200] and keys5[0] == p2_id, \
                f"req5 must still be served by p2 (probe saw 429): st={st5} keys={keys5}"
            print(f"OK: probe kept key1 cooled (req5 served by key2 {p2_id})")

            print("cooldown probe test passed")
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
