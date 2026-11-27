#!/usr/bin/env python3
"""Crash-recovery regression for the durable request-log spool.

A replay is only proven by its numbers, so every replayed row is compared
field-by-field against the values the live path would have written.  The
fixture prices the model: 11 prompt tokens at 2.0/1M plus 7 completion tokens
at 8.0/1M are 7.8e-05 CNY, so a frame whose token or cost fields shifted
cannot replay as a "successful" row.

  R1  one crash: the spooled frame replays into exactly ONE row whose tokens,
      cost, pricing status and requested_at (the crash-time instant, not the
      replay instant) match; request_attempts keeps exactly one row.
  R2  two crashes: each leaves one frame, and replaying them yields both rows
      in the original request order (ids and requested_at ascend) with the
      same numeric fidelity.
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
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from support import stop_process

CRASH_ENV = "TB_TEST_CRASH_AFTER_SPOOL_SYNC"

# A CNY rate so a replayed row carries a real, non-zero amount.
PRICING_RULE = (
    "INSERT INTO pricing_rules"
    "(id,model_pattern,priority,input_price,cache_read_price,output_price,"
    "currency) VALUES(1,'*',0,2.0,1.0,8.0,'CNY')"
)

# The mock's non-streaming OpenAI usage: 11 prompt, 7 completion.
EXPECTED_TOKENS = (11, 7, 0, 18)          # prompt, completion, cache_read, total
EXPECTED_COST = 11 / 1_000_000.0 * 2.0 + 7 / 1_000_000.0 * 8.0  # 7.8e-05
COST_EPSILON = 1e-12

ROW_COLUMNS = (
    "id,status_code,prompt_tokens,completion_tokens,cache_read_tokens,"
    "total_tokens,equivalent_cost,billed_usage_cost,pricing_status,is_streaming,"
    "attempt_count,requested_at"
)
ROW_QUERY = f"SELECT {ROW_COLUMNS} FROM request_log WHERE model=? ORDER BY id"
ALL_ROWS_QUERY = f"SELECT {ROW_COLUMNS} FROM request_log ORDER BY id"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def iso_now() -> str:
    """The exact shape SQLite writes: millisecond precision, trailing Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_second(stamp: str) -> str:
    """requested_at is stamped at second precision (suffix is always .000Z)."""
    return stamp[:19]


def wait_for_proxy(port: int, *, allow_degraded: bool = False) -> None:
    deadline = time.monotonic() + 8
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.3) as response:
                if response.status == 200:
                    return
        except urllib.error.HTTPError as error:
            if not allow_degraded or error.code != 503:
                raise
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError("proxy did not become healthy")


def start_proxy(binary: Path, db: Path, port: int, env: dict):
    return subprocess.Popen(
        [str(binary), "--db", str(db),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
    )


def spool_path(db: Path) -> Path:
    return Path(str(db) + ".request-log.spool")


def send_request(port: int, local_key: str, model: str) -> None:
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        # The injected _exit races the response write; the durable spool is
        # the contract under test.
        pass


def crash_once(binary: Path, db: Path, local_key: str,
               model: str) -> tuple[str, str]:
    """Crash one request at the spool boundary.

    Returns (request window start, crash wall clock).
    """
    port = free_port()
    env = os.environ.copy()
    env[CRASH_ENV] = "1"
    started_at = iso_now()
    process = start_proxy(binary, db, port, env)
    try:
        wait_for_proxy(port)
        send_request(port, local_key, model)
        deadline = time.monotonic() + 8
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.returncode == 86, process.returncode
        # Prove the crash really hit spool sync: the frame outlives the exit
        # while the row it describes was never committed.
        spool = spool_path(db)
        assert spool.exists() and spool.stat().st_size > 0
        assert model_rows(db, model) == [], (model, model_rows(db, model))
        assert attempt_rows(db, model) == 0, model
        return started_at, iso_now()
    finally:
        stop_process(process, allowed_returncodes=(0, -15, 86))


def replay_rows(binary: Path, db: Path, expected: int) -> list[dict]:
    """Start a clean proxy, wait for the replay, return every logged row."""
    port = free_port()
    process = start_proxy(binary, db, port, os.environ.copy())
    try:
        wait_for_proxy(port, allow_degraded=True)
        deadline = time.monotonic() + 10
        rows: list[dict] = []
        while time.monotonic() < deadline:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(row) for row in conn.execute(ALL_ROWS_QUERY)]
            finally:
                conn.close()
            if len(rows) >= expected:
                break
            time.sleep(0.05)
        wait_for_proxy(port)
        return rows
    finally:
        stop_process(process)


def model_rows(db: Path, model: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(ROW_QUERY, (model,))]
    finally:
        conn.close()


def attempt_rows(db: Path, model: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT count(*) FROM request_attempts a JOIN request_log r "
            "ON r.id=a.request_log_id WHERE r.model=?", (model,)).fetchone()[0]
    finally:
        conn.close()


def assert_replayed_row(db: Path, model: str, *, request_started_at: str,
                        crashed_at: str, replay_started_at: str) -> dict:
    """One row, exact token/cost fidelity, original timestamp."""
    rows = model_rows(db, model)
    assert len(rows) == 1, (model, rows)
    row = rows[0]
    observed = (
        row["status_code"], row["prompt_tokens"], row["completion_tokens"],
        row["cache_read_tokens"], row["total_tokens"], row["is_streaming"],
        row["attempt_count"], row["pricing_status"],
    )
    assert observed == (200, *EXPECTED_TOKENS, 0, 1, "rated"), (model, observed)
    assert abs(row["equivalent_cost"] - EXPECTED_COST) < COST_EPSILON, \
        (model, row["equivalent_cost"], EXPECTED_COST)
    # The account is metered, so the billed amount must equal the frozen
    # equivalent cost — a replayed row may not silently lose its billing.
    assert abs(row["billed_usage_cost"] - EXPECTED_COST) < COST_EPSILON, \
        (model, row["billed_usage_cost"], EXPECTED_COST)
    assert attempt_rows(db, model) == 1, model
    # requested_at is the usage instant, not the replay instant: it sits in
    # the crashed request's window and strictly pre-dates the replay run.
    stamp = iso_second(row["requested_at"])
    assert iso_second(request_started_at) <= stamp <= iso_second(crashed_at), \
        (model, row["requested_at"], request_started_at, crashed_at)
    assert stamp < iso_second(replay_started_at), \
        (model, row["requested_at"], replay_started_at)
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
    try:
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "token-board.db"
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                account, upstream_id, _ = add_upstream(
                    conn, "recovery", f"http://127.0.0.1:{upstream_port}",
                    ["sk-recovery"], max_concurrency=8)
                add_plain_route(conn, "tb-recovery", "recovery-route",
                                upstream_id, account)
                conn.execute(PRICING_RULE)
                conn.commit()
            finally:
                conn.close()

            # ── R1: one crash, one replayed frame ────────────────────────
            started_at, crash_at = crash_once(
                binary, db, "tb-recovery", "recovery-one")
            # Leave a whole second between the crash and the replay so the
            # "requested_at predates the replay" assertion is exact even
            # though requested_at carries second precision.
            time.sleep(1.2)
            replay_started_at = iso_now()
            replay_rows(binary, db, 1)
            assert_replayed_row(db, "recovery-one",
                                request_started_at=started_at,
                                crashed_at=crash_at,
                                replay_started_at=replay_started_at)
            # The frames were consumed, so the next crash starts from an
            # empty spool instead of crashing during startup recovery.
            assert spool_path(db).stat().st_size == 0, spool_path(db).stat().st_size

            # ── R2: a second crash replays after the first ──────────────
            second_started_at, second_crash_at = crash_once(
                binary, db, "tb-recovery", "recovery-two")
            time.sleep(1.2)
            second_replay_started_at = iso_now()
            rows = replay_rows(binary, db, 2)
            assert len(rows) == 2, rows
            first = assert_replayed_row(
                db, "recovery-one", request_started_at=started_at,
                crashed_at=crash_at, replay_started_at=replay_started_at)
            second = assert_replayed_row(
                db, "recovery-two", request_started_at=second_started_at,
                crashed_at=second_crash_at,
                replay_started_at=second_replay_started_at)
            # Replay preserves the original request order.
            assert first["id"] < second["id"], (first["id"], second["id"])
            assert first["requested_at"] <= second["requested_at"], \
                (first["requested_at"], second["requested_at"])
    finally:
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    print("accounting recovery passed")


if __name__ == "__main__":
    main()
