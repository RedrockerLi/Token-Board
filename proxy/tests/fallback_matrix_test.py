#!/usr/bin/env python3
"""Fallback-matrix experiments (A3/A4/B1/B2/B3) against mock_upstream.

Drives the real proxy with an aggregate topology (plan account with two keys
+ Deepseek-API fallback) and asserts the request_attempts chains:

  A3  no fallback account: all plan keys cooled → fast 429 with 0 attempts.
  A4  a fallback 502 gives that key a short provider-fault avoid; the
      immediate retry is a fast 429 with 0 attempts.
  B1  non-streaming 5xx fast-fail → [plan/500, plan/500, api/200].
  B2  streaming 429 pre-commit → buffered, not written to the client →
      [plan/429, plan/429, api/200]; the attempt error keeps the HTTP-status
      reason ("Upstream returned 429"), not a truncation label.
  B3  streaming commit-then-stall (zen/go "吐首token后停滞") → semantic idle
      504 after the configured idle timeout; because content was already
      committed, NO fallback to the Deepseek-API key (attempt_count=1).
      This is review #5's baseline — measured with a short idle timeout here.

The C section covers stream terminal shapes on single-account routes (the
Anthropic-wire ones drive the Claude Code /v1/messages passthrough):

  C1  OpenAI upstream ending with [DONE] but no finish_reason → complete
      stream, no error frame, 200/[200].
  C2  Anthropic upstream ending with message_stop but no stop_reason in
      message_delta → complete stream, no error frame, 200/[200].
  C3  terminal frame without its trailing blank line AND no stop_reason in
      message_delta (so the EOF flush is the only terminal) → flushed and
      frame-terminated at EOF; complete, no error frame, 200/[200].
  C4  clean 200 with an empty body (terminal-less, nothing committed) →
      classified truncated; 502 is retryable pre-commit, so both keys are
      attempted: chain [502, 502].
  C5  content streamed then a clean, terminal-less end (committed) → the
      client keeps the content plus an error frame; request_log/attempts
      record 502 and there is NO fallback (attempt_count=1).
  C6  an immediate retry on a single-key route whose only key sits in the
      30s provider-fault window after C5's truncation → clean 0-attempt 503
      ("All upstream keys are temporarily unavailable"), never a hung
      connection.
  C7  a streaming request while the only slot is occupied → clean 0-attempt
      503 ("All upstream key concurrency slots are occupied"), never a hung
      connection.
  C8  every frame delivered but the body closed without the chunked
      terminator (sloppy upstream close) → still a complete 200 stream, no
      error frame, no key fault.

Usage:
  python3 fallback_matrix_test.py <token_proxy> <schema_dir> <project_root>
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


def build_scenario(conn, local_key, model, plan_keys, deepseek_key=None):
    """plan account (plan_keys) + optional deepseek account, behind aggregate."""
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
    except Exception as e:  # noqa: BLE001 — e.g. truncated stream (B3)
        return 0


def send_path(proxy_port, local_key, path, body, timeout=30):
    """POST `body` to `path` and return (status, decoded response text)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {local_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a stream without a proper ending
        return 0, ""


def attempt_rows(db_path, request_log_id):
    """(status_code, error) of each attempt of a request, in attempt order."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT status_code, error FROM request_attempts "
            "WHERE request_log_id=? ORDER BY attempt_index",
            (request_log_id,)).fetchall()
    finally:
        conn.close()


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
            build_scenario(conn, "tb-a3", "a3-model", ["sk-a3-p1", "sk-a3-p2"])
            build_scenario(conn, "tb-a4", "a4-model", ["sk-a4-p1", "sk-a4-p2"], "sk-a4-d")
            build_scenario(conn, "tb-b1", "b1-model", ["sk-b1-p1", "sk-b1-p2"], "sk-b1-d")
            build_scenario(conn, "tb-b2", "b2-model", ["sk-b2-p1", "sk-b2-p2"], "sk-b2-d")
            build_scenario(conn, "tb-b3", "b3-model", ["sk-b3-p1"], "sk-b3-d")
            # C-scenarios: single-account routes for stream terminal shapes.
            # The anthropic-wire accounts drive the Claude Code passthrough.
            from v1_fixture import add_plain_route, add_upstream
            for local_key, keys, api_format in (
                ("tb-c1", ["sk-c1-p1"], "openai"),
                ("tb-c2", ["sk-c2-p1"], "anthropic"),
                ("tb-c3", ["sk-c3-p1"], "anthropic"),
                ("tb-c4", ["sk-c4-p1", "sk-c4-p2"], "anthropic"),
                ("tb-c5", ["sk-c5-p1", "sk-c5-p2"], "anthropic"),
                ("tb-c6", ["sk-c6-p1"], "anthropic"),
                ("tb-c8", ["sk-c8-p1"], "anthropic"),
            ):
                c_account, c_upstream, _ = add_upstream(
                    conn, local_key, "", keys, api_format=api_format)
                add_plain_route(conn, local_key, f"{local_key}-route",
                                c_upstream, c_account)
            # C7: a single in-flight slot, for the local-capacity 0-attempt path.
            c7_account, c7_upstream, _ = add_upstream(
                conn, "tb-c7", "", ["sk-c7-p1"], api_format="anthropic",
                max_concurrency=1)
            add_plain_route(conn, "tb-c7", "tb-c7-route", c7_upstream,
                            c7_account)
            conn.execute("UPDATE upstreams SET base_url=?", (base_url,))
            # Fast B3 baseline: shrink the OpenAI-format semantic idle timeout
            # so the commit-then-stall → 504 round-trip completes in seconds.
            conn.execute(
                "UPDATE proxy_timeout_config SET "
                "streaming_first_byte_timeout=10, streaming_idle_timeout=4 "
                "WHERE endpoint_kind='chat'")
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--host", "127.0.0.1",
             "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            env=os.environ.copy())
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

            # ── A4: a fallback 502 faults that key for a short avoid ──
            send(proxy_port, "tb-a4", {**common, "model": "a4-model", "stream": False,
                "mock_status_by_key": {"sk-a4-p1": 429, "sk-a4-p2": 429, "sk-a4-d": 502},
                "mock_error_type_by_key": {"sk-a4-p1": "GoUsageLimitError",
                                           "sk-a4-p2": "GoUsageLimitError"}})
            rid, _, st = latest_attempts(db_path, "a4-model")
            assert st == [429, 429, 502], f"A4 req1 chain {st}"
            # req2 (immediate): both plan keys are in their retained quota
            # cooldown and the fallback carries the provider-fault avoid the
            # 502 created — nothing is tried, and the client gets the
            # provider-cooldown 429 without touching the upstream.
            status = send(proxy_port, "tb-a4", {**common, "model": "a4-model", "stream": False})
            _, n2, st2 = latest_attempts(db_path, "a4-model", rid)
            assert n2 == 0 and st2 == [], f"A4 req2 must be a fast 429: {n2=} {st2}"
            assert status == 429, f"A4 req2 status {status}"
            print(f"A4 OK: fallback 502 -> short provider-fault avoid, "
                  f"immediate retry is a fast 429 (chain1={st})")

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
            rows = attempt_rows(db_path, rid)
            assert rows[0][1] == "Upstream returned 429", (
                f"B2 attempt error must keep the HTTP-status reason: {rows[0]}")
            print(f"B2 OK: streaming 429 pre-commit buffered -> fallback ({st}), "
                  f"error kept as HTTP status")

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

            # ── C: stream terminal shapes ────────────────────────────────
            def anthropic_body(model, extra):
                return {"model": model, "max_tokens": 16, "stream": True,
                        "messages": [{"role": "user", "content": "hi"}],
                        **extra}

            # C1: [DONE] without finish_reason is still a complete stream.
            status, text = send_path(
                proxy_port, "tb-c1", "/v1/chat/completions",
                {**common, "model": "c1-model", "stream": True,
                 "mock_omit_finish_reason": True})
            assert status == 200, f"C1 status {status}"
            assert "[DONE]" in text, f"C1 must complete the stream: {text[-200:]}"
            assert '"error"' not in text, f"C1 must not surface an error: {text[-300:]}"
            _, n, st = latest_attempts(db_path, "c1-model")
            assert st == [200] and n == 1, f"C1 chain {st}"
            print("C1 OK: [DONE]-only OpenAI stream completes without error frame")

            # C2: message_stop without stop_reason is still a complete stream.
            status, text = send_path(
                proxy_port, "tb-c2", "/v1/messages",
                anthropic_body("c2-model", {"mock_omit_stop_reason": True}))
            assert status == 200, f"C2 status {status}"
            assert "event: message_stop" in text, f"C2 missing message_stop: {text[-300:]}"
            assert "event: error" not in text, f"C2 must not surface an error: {text[-300:]}"
            _, n, st = latest_attempts(db_path, "c2-model")
            assert st == [200] and n == 1, f"C2 chain {st}"
            print("C2 OK: message_stop-only Anthropic stream completes without error frame")

            # C3: the ONLY terminal is the trailing message_stop, stripped of
            # its blank line (stop_reason null) — the EOF flush must rescue
            # it, the proxy must re-terminate the dangling frame for the
            # client, and the stream stays a complete success.
            status, text = send_path(
                proxy_port, "tb-c3", "/v1/messages",
                anthropic_body("c3-model", {"mock_omit_stop_reason": True,
                                            "mock_strip_final_blank": True}))
            assert status == 200, f"C3 status {status}"
            assert "event: error" not in text, f"C3 must not surface an error: {text[-300:]}"
            assert text.endswith('"message_stop"}\n\n'), (
                f"C3 EOF-flushed terminal must be frame-terminated: {text[-200:]!r}")
            _, n, st = latest_attempts(db_path, "c3-model")
            assert st == [200] and n == 1, f"C3 chain {st}"
            print("C3 OK: EOF-flushed terminal frame rescued and frame-terminated")

            # C4: clean 200 with an empty body — truncated with nothing
            # committed, so 502 is retryable and both keys are attempted.
            status, text = send_path(
                proxy_port, "tb-c4", "/v1/messages",
                anthropic_body("c4-model", {"mock_empty_stream": True}))
            assert status == 502, f"C4 status {status}"
            assert "truncated" in text, f"C4 error body: {text[-300:]}"
            _, n, st = latest_attempts(db_path, "c4-model")
            assert st == [502, 502] and n == 2, f"C4 chain {st} attempts {n}"
            print("C4 OK: uncommitted truncation is 502 and falls back (chain [502, 502])")

            # C5: content streamed, then a clean terminal-less end.  The
            # client keeps the content plus an error frame; the request is
            # recorded as 502 and cannot fall back post-commit (the route has
            # a second, reachable key precisely so attempt_count==1 proves
            # the no-fallback invariant rather than the topology).
            status, text = send_path(
                proxy_port, "tb-c5", "/v1/messages",
                anthropic_body("c5-model", {"mock_omit_stop_reason": True,
                                            "mock_omit_terminal": True}))
            assert status == 200, f"C5 status {status}"
            assert "event: error" in text and "truncated" in text, (
                f"C5 must surface a truncation error: {text[-300:]}")
            _, n, st = latest_attempts(db_path, "c5-model")
            assert st == [502] and n == 1, f"C5 chain {st} attempts {n}"
            print("C5 OK: committed truncation -> error frame, recorded 502, no fallback")

            # C6: the truncated key sits out its 30s provider-fault window; an
            # immediate retry on a single-key route has no eligible candidate
            # and must get a clean 0-attempt 503 (never a hung connection).
            status, text = send_path(
                proxy_port, "tb-c6", "/v1/messages",
                anthropic_body("c6-model", {"mock_omit_stop_reason": True,
                                            "mock_omit_terminal": True}))
            assert status == 200 and "event: error" in text, (status, text[-200:])
            rid6, n6, st6 = latest_attempts(db_path, "c6-model")
            assert st6 == [502] and n6 == 1, f"C6 truncation chain {st6}"
            status, text = send_path(
                proxy_port, "tb-c6", "/v1/messages", anthropic_body("c6-model", {}))
            assert status == 503, f"C6 retry status {status}"
            assert "temporarily unavailable" in text, f"C6 retry body: {text[-300:]}"
            _, n6b, st6b = latest_attempts(db_path, "c6-model", rid6)
            assert n6b == 0 and st6b == [], f"C6 retry must be 0-attempt: {n6b} {st6b}"
            print("C6 OK: retry inside the provider-fault window -> clean 0-attempt 503")

            # C7: while the only slot is occupied by an in-flight stream, a
            # second streaming request has no capacity and must get a clean
            # 0-attempt 503 (never a hung connection).
            holder = {}
            def hold_slot():
                holder["r"] = send_path(
                    proxy_port, "tb-c7", "/v1/messages",
                    anthropic_body("c7-model", {"mock_stall_after": 1,
                                                "mock_stall_secs": 6.0}),
                    timeout=30)
            holder_thread = threading.Thread(target=hold_slot)
            holder_thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{proxy_port}/health", timeout=0.5) as r:
                    if json.loads(r.read())["concurrency"] >= 1:
                        break
                time.sleep(0.02)
            else:
                raise AssertionError("C7 holder never occupied the slot")
            status, text = send_path(
                proxy_port, "tb-c7", "/v1/messages", anthropic_body("c7-model", {}))
            assert status == 503, f"C7 status {status}"
            assert "concurrency slots" in text, f"C7 body: {text[-300:]}"
            holder_thread.join(timeout=30)
            assert holder.get("r", (0, ""))[0] == 200, f"C7 holder {holder.get('r', (0, ''))[0]}"
            print("C7 OK: streaming local-capacity exhaustion -> clean 0-attempt 503")

            # C8: every frame delivered, but the body closes without the
            # chunked terminator — still a complete stream (no error frame,
            # no key fault), never a 502 read-failure.
            status, text = send_path(
                proxy_port, "tb-c8", "/v1/messages",
                anthropic_body("c8-model", {"mock_omit_chunk_terminator": True}))
            assert status == 200, f"C8 status {status}"
            assert "event: message_stop" in text, f"C8 missing message_stop: {text[-200:]}"
            assert "event: error" not in text, f"C8 must not surface an error: {text[-300:]}"
            _, n, st = latest_attempts(db_path, "c8-model")
            assert st == [200] and n == 1, f"C8 chain {st}"
            print("C8 OK: sloppy close without the chunked terminator stays a success")

            print("fallback matrix tests passed")
        finally:
            diagnostics = stop_process(proxy, check_returncode=False)
            if proxy.returncode not in (0, -15):
                print(diagnostics, file=sys.stderr)

    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
