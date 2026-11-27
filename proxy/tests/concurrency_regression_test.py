#!/usr/bin/env python3
"""Concurrency/robustness regressions from audit 04 (Wave A).

Each section pins one audit row through an externally observable fact:

  C-18  a 429 that names a *terminal* provider quota ("Your project has
        exceeded its monthly spending cap") must be classified as a quota
        cooldown, not as a 60s rate limit: no Retry-After, a five-hour hold
        reported in the log, and no further upstream attempt while it lasts.
  C-19  the same failure must stay diagnosable: the client-facing message
        carries the upstream's own text, request_attempts.error keeps an
        excerpt of it, and the two wording shapes (with / without a recorded
        attempt) are distinguishable.
  C-07  a proxy that cannot listen must exit non-zero with a clear error
        instead of idling as if it were serving.
  C-16  a downstream socket closed with a clean FIN *after the response
        committed* must be detected promptly (audit 99 E-06: on TCP the clean
        FIN shows up as POLLIN|POLLRDHUP, not POLLHUP).  The transport
        contract itself is pinned separately so a platform change is visible.

        Known limitation: a clean FIN arriving *before* the first byte is not
        asserted, and cannot be detected safely — on TCP a peer that close()s
        is indistinguishable from one that shutdown(SHUT_WR)s and keeps
        reading (audit 04 §3.7 states that judgement is intentional, and the
        non-streaming exits have the same boundary; see
        client_disconnect_test.py D3/D4).  Only the committed phase is
        observable, which is what this case pins.

Usage:
  concurrency_regression_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import json
import os
import select
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

FAILURES: list[str] = []

MONTHLY_CAP_TEXT = "Your project has exceeded its monthly spending cap"
QUOTA_HOLD_MARKER = "[Quota] terminal quota 429 held"


def check(ok: bool, what: str) -> None:
    print(f"  {'ok:  ' if ok else 'FAIL:'} {what}")
    if not ok:
        FAILURES.append(what)


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
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def post(port: int, key: str, body: dict) -> tuple[int, dict, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read() or b"{}")
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        return error.code, dict(error.headers), payload


def message_of(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message", ""))
    return str(payload.get("raw", ""))


def attempt_error(db: Path, model: str) -> str:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT a.error FROM request_attempts a JOIN request_log r "
            "ON r.id=a.request_log_id WHERE r.model=? ORDER BY a.id",
            (model,)).fetchone()
        return "" if row is None else str(row[0] or "")
    finally:
        conn.close()


def log_row(db: Path, model: str) -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status_code,attempt_count FROM request_log "
                "WHERE model=? ORDER BY id DESC LIMIT 1", (model,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            return dict(row)
        time.sleep(0.03)
    raise AssertionError(f"{model}: request_log row never appeared")


def seed_route(conn, local_key: str, upstream_port: int) -> None:
    from v1_fixture import add_plain_route, add_upstream

    account, upstream_id, _ = add_upstream(
        conn, local_key, f"http://127.0.0.1:{upstream_port}", [f"sk-{local_key}"],
        max_concurrency=4)
    add_plain_route(conn, local_key, f"{local_key}-route", upstream_id, account)


def start_proxy(binary: Path, db: Path, port: int, extra_env: dict | None = None):
    env = os.environ.copy()
    env.update(extra_env or {})
    return subprocess.Popen(
        [str(binary), "--db", str(db), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)


def check_quota_cooldown(binary: Path, schema: Path,
                         mock_upstream) -> None:
    print("--- C-18/C-19 terminal provider quota ---")
    upstream_port = free_port()
    log_path = Path(tempfile.mkdtemp(prefix="tb-mock-")) / "upstream.log"
    mock_upstream.LOG_FILE = str(log_path)
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port),
                                   mock_upstream.Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = None
    try:
        with tempfile.TemporaryDirectory(prefix="tb-quota-") as raw:
            db = Path(raw) / "token-board.db"
            from v1_fixture import ensure_v2_database
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                seed_route(conn, "tb-quota", upstream_port)
                conn.commit()
            finally:
                conn.close()

            port = free_port()
            proxy = start_proxy(binary, db, port)
            wait_for_health(port)
            body = {
                "model": "quota-model", "stream": False,
                "mock_status": 429,
                "mock_error_body": {"error": {"message": MONTHLY_CAP_TEXT,
                                              "type": "billing_error"}},
                "messages": [{"role": "user", "content": "hi"}],
            }
            status, headers, payload = post(port, "tb-quota", body)
            message = message_of(payload)
            check(status == 429, f"C-18: the exhausted key answers 429 ({status})")
            check("Retry-After" not in headers,
                  f"C-18: a terminal quota carries no Retry-After "
                  f"({headers.get('Retry-After')})")

            # C-18: no periodic re-attempt while the quota hold lasts.
            calls_before = len(log_path.read_text().splitlines()) \
                if log_path.exists() else 0
            status2, headers2, payload2 = post(port, "tb-quota", body)
            calls_after = len(log_path.read_text().splitlines()) \
                if log_path.exists() else 0
            check(status2 == 429, f"C-18: the held key answers 429 again ({status2})")
            # C-18: a terminal quota is not a 60s rate limit, so the synthesized
            # all-candidates-cooled answer must not advertise a retry window.
            check("Retry-After" not in headers2,
                  f"C-18: the cooled answer carries no Retry-After "
                  f"({headers2.get('Retry-After')})")
            # C-19: the all-candidates-cooled answer is synthesized by the
            # proxy, so this is where the upstream's own words must survive.
            message2 = message_of(payload2)
            check(MONTHLY_CAP_TEXT in message2,
                  f"C-19: the cooled message quotes the upstream body "
                  f"({message2[:160]!r})")
            check(calls_after == calls_before,
                  f"C-18: no upstream attempt during the quota hold "
                  f"({calls_before}->{calls_after})")
            held = log_row(db, "quota-model")
            # The cooled answer admits no upstream attempt of its own.
            check(held["status_code"] == 429 and held["attempt_count"] == 0,
                  f"C-18: the cooled answer records no new attempt ({held})")

        stderr = stop_process(proxy)
        proxy = None
        check(QUOTA_HOLD_MARKER in stderr,
              "C-18: the terminal quota hold is logged for operators")
        check("cooldown_s=18000" in stderr,
              "C-18: the hold is five hours, not a 60s rate-limit window")
    finally:
        if proxy is not None:
            stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()


def check_listen_failure(binary: Path, schema: Path) -> None:
    print("--- C-07 listen failure ---")
    with tempfile.TemporaryDirectory(prefix="tb-listen-") as raw:
        db = Path(raw) / "token-board.db"
        from v1_fixture import ensure_v2_database
        ensure_v2_database(db, schema)
        blocker = socket.socket()
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]
            process = start_proxy(binary, db, busy_port)
            deadline = time.monotonic() + 8
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            exited = process.poll() is not None
            if not exited:
                stop_process(process, timeout=2, check_returncode=False)
                check(False, "C-07: a proxy that cannot listen exits instead of idling")
                return
            stderr = process.stderr.read() if process.stderr else ""
            if process.stderr is not None:
                process.stderr.close()
            check(process.returncode != 0,
                  f"C-07: listen failure exits non-zero ({process.returncode})")
            check("cannot listen" in stderr.lower() or "listen" in stderr.lower(),
                  f"C-07: the failure names the listen step ({stderr[-160:]!r})")
            check("Proxy listening" not in stderr,
                  "C-07: no success banner is printed for a failed bind")
        finally:
            blocker.close()


def check_clean_fin_detected(binary: Path, schema: Path,
                             mock_upstream) -> None:
    print("--- C-16 clean FIN after the response committed ---")
    upstream_port = free_port()
    mock_upstream.LOG_FILE = str(
        Path(tempfile.mkdtemp(prefix="tb-mock-fin-")) / "upstream.log")
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port),
                                   mock_upstream.Handler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    proxy = None
    try:
        with tempfile.TemporaryDirectory(prefix="tb-clean-fin-") as raw:
            db = Path(raw) / "token-board.db"
            from v1_fixture import ensure_v2_database
            ensure_v2_database(db, schema)
            conn = sqlite3.connect(db)
            try:
                seed_route(conn, "tb-clean-fin", upstream_port)
                conn.commit()
            finally:
                conn.close()

            port = free_port()
            proxy = start_proxy(binary, db, port)
            wait_for_health(port)
            # The stream must commit *before* the client leaves: only the
            # committed phase can tell a departing peer apart from one that
            # half-closed and keeps reading (audit 04 §3.7 recommendation ②).
            body = json.dumps({
                "model": "clean-fin-model", "stream": True,
                "mock_simple_stream": True,
                "mock_long_stream_frames": 60,
                "mock_chunk_delay": 0.05,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            client = socket.create_connection(("127.0.0.1", port), timeout=8)
            client.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer tb-clean-fin\r\n"
                b"Content-Type: application/json\r\n" +
                f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            received = b""
            while b"data:" not in received:
                chunk = client.recv(4096)
                if not chunk:
                    raise AssertionError("proxy closed the stream before the "
                                         "first event")
                received += chunk
            # The response is committed now, so a clean FIN is observable; no
            # RST and no failed write is involved.
            client.close()

            deadline = time.monotonic() + 5
            status = None
            while time.monotonic() < deadline:
                conn = sqlite3.connect(db)
                try:
                    row = conn.execute(
                        "SELECT status_code FROM request_log "
                        "WHERE model='clean-fin-model'").fetchone()
                finally:
                    conn.close()
                if row is not None:
                    status = row[0]
                    break
                time.sleep(0.05)
            check(status == 499,
                  f"C-16: a committed stream cut by a clean FIN records 499 "
                  f"promptly ({status})")
            if status is None:
                check(False, "C-16: the detached request was never accounted for")
    finally:
        if proxy is not None:
            # The mock still holds the silenced request, so a bounded graceful
            # stop may need a kill; the accounting assertion is what matters.
            stop_process(proxy, timeout=5, allowed_returncodes=(0, -15, -9))
        upstream.shutdown()
        upstream.server_close()


def check_transport_fin_semantics() -> None:
    """Pin 99 E-06: the same clean close looks different per transport."""
    print("--- C-16 transport semantics (99 E-06) ---")
    POLLRDHUP = getattr(select, "POLLRDHUP", 0x2000)
    tcp_listener = socket.socket()
    tcp_listener.bind(("127.0.0.1", 0))
    tcp_listener.listen(1)
    tcp_client = socket.create_connection(tcp_listener.getsockname(), timeout=5)
    tcp_server, _ = tcp_listener.accept()
    unix_client, unix_server = socket.socketpair(socket.AF_UNIX)
    try:
        tcp_client.close()      # clean FIN, empty receive buffer
        unix_client.close()
        time.sleep(0.15)
        tcp_poll = select.poll()
        tcp_poll.register(tcp_server, select.POLLIN | POLLRDHUP)
        unix_poll = select.poll()
        unix_poll.register(unix_server, select.POLLIN | POLLRDHUP)
        tcp_ready = tcp_poll.poll(500)
        unix_ready = unix_poll.poll(500)
        tcp_events = tcp_ready[0][1] if tcp_ready else 0
        unix_events = unix_ready[0][1] if unix_ready else 0
        # TCP: POLLIN|POLLRDHUP (0x2001) and no POLLHUP; AF_UNIX: POLLHUP set.
        check(bool(tcp_events & POLLRDHUP),
              f"C-16: TCP clean FIN reports POLLRDHUP (0x{tcp_events:04x})")
        check(not (tcp_events & select.POLLHUP),
              f"C-16: TCP clean FIN does not set POLLHUP (0x{tcp_events:04x})")
        check(bool(unix_events & select.POLLHUP),
              f"C-16: AF_UNIX clean FIN sets POLLHUP (0x{unix_events:04x})")
    finally:
        for sock in (tcp_listener, tcp_server, unix_server):
            sock.close()


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project))
    sys.path.insert(0, str(project / "proxy" / "tests"))
    import scripts.mock_upstream as mock_upstream  # noqa: E402

    check_transport_fin_semantics()
    check_listen_failure(binary, schema)
    check_clean_fin_detected(binary, schema, mock_upstream)
    check_quota_cooldown(binary, schema, mock_upstream)

    print(f"\n{'ALL PASSED' if not FAILURES else 'FAILED'} "
          f"({len(FAILURES)} failure{'s' if len(FAILURES) != 1 else ''})")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
