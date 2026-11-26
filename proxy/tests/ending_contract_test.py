#!/usr/bin/env python3
"""An ending plan contract is still the account's live contract.

`end_of_period` only writes `billing_contracts.ends_at`; until that boundary the
contract still bills the account as a subscription and still supplies its
cooldown policy.  Two runtime readers are pinned here:

  B1  the request_log writer keeps the billing unit, contract uuid and anchor
      day attributed to the ending contract (the SQLite `price_usage_event`
      trigger settles `billed_usage_cost` afterwards from the metered-contract
      window, so it stays 0 for a subscription either way);
  B2  a GoUsageLimitError 429 on such a key still takes the subscription
      cooldown branch.  That branch holds the slot in `cooldown_until`, not in
      the short-lived rate-limit avoid, so the next all-candidates-cooled 429
      carries no Retry-After; a key that degraded to an ordinary 429 reports
      the upstream's Retry-After instead.

Both scenarios set `ends_at` far in the future, exactly like a deferred
deletion does.  B2 runs a live contract as the control, so the assertion says
"an ending contract behaves like a live one" rather than hard-coding a value.
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
from http.server import ThreadingHTTPServer
from pathlib import Path

from support import sqlite_connection, stop_process

ENDING_AT = "2099-01-01T00:00:00Z"


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
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def send(port: int, local_key: str, body: dict) -> tuple[int, dict]:
    """POST one chat request and return (status, headers)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
            return response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, dict(error.headers)


def latest_log(db_path: Path, min_id: int) -> sqlite3.Row:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with sqlite_connection(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id,status_code,equivalent_cost,billed_usage_cost,"
                "billing_unit_id,billing_contract_uuid,billing_anchor_day "
                "FROM request_log WHERE id>? ORDER BY id LIMIT 1", (min_id,)
            ).fetchone()
        if row is not None:
            return row
        time.sleep(0.03)
    raise AssertionError("request-log row never appeared")


def latest_attempts(db_path: Path, model: str, min_id: int = 0):
    """(row_id, attempt_count, [status codes]) of the newest row for `model`."""
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with sqlite_connection(db_path) as conn:
            row = conn.execute(
                "SELECT id,attempt_count FROM request_log WHERE model=? AND id>? "
                "ORDER BY id DESC LIMIT 1", (model, min_id)).fetchone()
            if row is not None:
                attempts = conn.execute(
                    "SELECT status_code FROM request_attempts "
                    "WHERE request_log_id=? ORDER BY attempt_index", (row[0],)
                ).fetchall()
                return row[0], row[1], [item[0] for item in attempts]
        time.sleep(0.03)
    raise AssertionError(f"no request_log row for {model}")


def seed_pricing(conn) -> None:
    """A CNY rate so the equivalent cost is a real, non-zero number."""
    conn.execute(
        "INSERT INTO pricing_rules"
        "(id,model_pattern,priority,input_price,cache_read_price,output_price,"
        "currency) VALUES(1,'*',0,2.0,1.0,8.0,'CNY')")


def add_single_account_route(conn, name: str, base_url: str, local_key: str,
                             model: str):
    """One plan account behind its own aggregate route set; returns the ids."""
    from v1_fixture import add_upstream

    account_id, upstream_id, _ = add_upstream(
        conn, name, base_url, [f"sk-{name}"], recurring=True)
    route_set_id = conn.execute(
        "SELECT COALESCE(MAX(id),0)+1 FROM route_sets").fetchone()[0]
    conn.execute(
        "INSERT INTO route_sets(id,uuid,name) VALUES(?,?,?)",
        (route_set_id, f"{name}-{route_set_id}", name))
    conn.execute(
        "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id,"
        "target_model) VALUES(?,?,0,?,?)",
        (route_set_id, model, upstream_id, model))
    conn.execute(
        "INSERT INTO client_keys(uuid,key_value,label,route_set_id) "
        "VALUES(?,?,?,?)", (f"{name}-key", local_key, local_key, route_set_id))
    return account_id, route_set_id


def observe_quota_cooldown(proxy_port: int, db_path: Path, local_key: str,
                           upstream_key: str, model: str) -> tuple[int, object]:
    """Drive one quota-429 key and report how the next request is refused.

    Returns `(attempts, retry_after)` of the follow-up request.  The attempt
    count races the gate's mark latency and is informational; the Retry-After
    is the discriminating value — a key held by the subscription cooldown
    reports none, while a key that degraded to the ordinary rate-limit avoid
    reports the upstream's value.
    """
    quota_429 = {
        "mock_status_by_key": {upstream_key: 429},
        "mock_error_type_by_key": {upstream_key: "GoUsageLimitError"},
        "mock_retry_after": 1}

    def body():
        return {"model": model, "stream": False,
                "messages": [{"role": "user", "content": "hi"}], **quota_429}

    status, _ = send(proxy_port, local_key, body())
    assert status == 429, f"{local_key}: first request must surface the 429 ({status})"
    row_id, _, chain = latest_attempts(db_path, model)
    assert chain == [429], f"{local_key}: first request chain {chain}"

    time.sleep(0.25)
    status, headers = send(proxy_port, local_key, body())
    assert status == 429, f"{local_key}: second request status {status}"
    _, attempts, _ = latest_attempts(db_path, model, row_id)
    return attempts, headers.get("Retry-After")


def main() -> None:
    proxy_binary = Path(sys.argv[1]).resolve()
    schema_dir = Path(sys.argv[2]).resolve()
    project_root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project_root))

    from scripts.mock_upstream import Handler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    try:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "token-board.db"
            ensure_v2_database(db_path, schema_dir)
            base_url = f"http://127.0.0.1:{upstream_port}"
            with sqlite_connection(db_path) as conn:
                seed_pricing(conn)
                # B1: a plan account alone, so the logged row is its own.
                b1_account, b1_upstream, _ = add_upstream(
                    conn, "ending-b1", base_url, ["sk-ending-b1"], recurring=True)
                add_plain_route(conn, "tb-b1", "ending-b1-route", b1_upstream,
                                b1_account)
                # B2: one ending contract and one live control, same shape.
                ending_account, _ = add_single_account_route(
                    conn, "ending", base_url, "tb-ending", "ending-model")
                add_single_account_route(
                    conn, "live", base_url, "tb-live", "live-model")
                conn.execute(
                    "UPDATE billing_contracts SET ends_at=? WHERE account_id IN (?,?)",
                    (ENDING_AT, b1_account, ending_account))
                conn.commit()

            proxy_port = free_port()
            proxy = subprocess.Popen(
                [str(proxy_binary), "--db", str(db_path), "--host", "127.0.0.1",
                 "--port", str(proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=os.environ.copy())
            try:
                wait_for_health(proxy_port)

                # ── B1: the ending contract still owns the billing facts ──
                status, _ = send(proxy_port, "tb-b1",
                                 {"model": "b1-model", "stream": False,
                                  "messages": [{"role": "user", "content": "hi"}]})
                assert status == 200, status
                row = latest_log(db_path, 0)
                assert row["status_code"] == 200, dict(row)
                assert row["equivalent_cost"] > 0, \
                    f"a non-zero equivalent cost makes the check real: {dict(row)}"
                # `price_usage_event` settles the per-request billed cost from
                # the *metered* contract window, so a subscription row stays 0
                # whether or not the ending contract is attributed.
                assert row["billed_usage_cost"] == 0, \
                    f"a subscription period must not bill per request: {dict(row)}"
                assert row["billing_unit_id"], \
                    f"the ending contract still owns the billing unit: {dict(row)}"
                assert row["billing_contract_uuid"], \
                    f"the ending contract must remain attributed: {dict(row)}"
                assert row["billing_anchor_day"] is not None, \
                    f"the ending contract still supplies the anchor: {dict(row)}"
                print("B1 OK: an ending plan contract keeps its billing attribution")

                # ── B2: the live control first, then the ending contract ──
                # The gate applies a mark a moment after the attempt
                # completes, so the observation is differential: an ending
                # contract must refuse exactly like a live one, and a live one
                # reports no rate-limit Retry-After.
                live = observe_quota_cooldown(
                    proxy_port, db_path, "tb-live", "sk-live", "live-model")
                ending = observe_quota_cooldown(
                    proxy_port, db_path, "tb-ending", "sk-ending", "ending-model")
                assert live[1] is None, (
                    "the live control must take the subscription cooldown path "
                    f"(attempts={live[0]}, Retry-After={live[1]!r})")
                # Only the refusal shape is compared: the attempt count of the
                # follow-up request races the gate's mark latency.
                assert ending[1] == live[1], (
                    "an ending contract must cool its key exactly like a live "
                    f"one: ending={ending} live={live}")
                print("B2 OK: an ending plan key keeps the subscription cooldown")
            finally:
                stop_process(proxy)
    finally:
        upstream.shutdown()


if __name__ == "__main__":
    main()
