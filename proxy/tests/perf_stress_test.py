#!/usr/bin/env python3
"""Proxy throughput / latency stress test against mock_upstream.

Drives the REAL token_proxy binary with a temp token-board.db and an in-process mock
upstream (scripts/mock_upstream), measures client-side latency plus the
proxy's own request_log upstream_ttft_ms / duration_ms, and prints p50/p95/p99.

Connection-reuse is deterministically proven by upstream_client_perf (the unit
gate); this script is the end-to-end load test: throughput + latency
distribution + attempt-chain fidelity under concurrency, runnable Before/After
a change for comparison.

Usage:
  python3 perf_stress_test.py <token_proxy> <schema_dir> <project_root>
      [--requests N] [--concurrency C] [--no-stream] [--tag LABEL]
      [--mock-stall-after N] [--mock-stall-secs S]   # optional stall scenario
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


class LoadHTTPServer(ThreadingHTTPServer):
    # The benchmark measures proxy queueing, not the stdlib test server's
    # default five-entry SYN backlog. Keep it above the tested concurrency.
    request_queue_size = 512


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 5
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.03)
    raise AssertionError("proxy did not become healthy")


def percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1,
                           int(len(sorted_vals) * pct / 100.0))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("token_proxy", type=str)
    ap.add_argument("schema_dir", type=str)
    ap.add_argument("project_root", type=str)
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--no-stream", action="store_true", default=False,
                    help="drive non-streaming chat completions")
    ap.add_argument("--tag", default="stress")
    ap.add_argument("--mock-stall-after", type=int, default=None,
                    help="streaming: pause after this SSE frame (commit-then-stall)")
    ap.add_argument("--mock-stall-secs", type=float, default=5)
    ap.add_argument("--min-rps", type=float, default=0,
                    help="fail when measured throughput is below this gate")
    ap.add_argument("--max-p95", type=float, default=0,
                    help="fail when client p95 exceeds this gate (ms)")
    ap.add_argument("--max-queue-p95", type=float, default=0,
                    help="fail when /health queue p95 exceeds this gate (ms)")
    args = ap.parse_args()

    proxy_binary = Path(args.token_proxy).resolve()
    schema_dir = Path(args.schema_dir).resolve()
    project_root = Path(args.project_root).resolve()
    sys.path.insert(0, str(project_root))
    from scripts.mock_upstream import Handler as MockHandler
    from v1_fixture import ensure_v2_database

    upstream_port = free_port()
    upstream = LoadHTTPServer(("127.0.0.1", upstream_port), MockHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        try:
            from v1_fixture import add_plain_route, add_upstream
            account_id, upstream_id, _ = add_upstream(
                conn, "mock", f"http://127.0.0.1:{upstream_port}",
                ["sk-mock-1", "sk-mock-2"], max_concurrency=256)
            add_plain_route(conn, "tb-stress", "stress", upstream_id,
                            account_id)
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(proxy_binary), "--db", str(db_path), "--schema-dir",
             str(schema_dir), "--host", "127.0.0.1", "--port", str(proxy_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            wait_for_health(proxy_port)
            base_body = {
                "model": "stress-model",
                "stream": not args.no_stream,
                "messages": [{"role": "user", "content": "hi"}],
            }
            if args.mock_stall_after is not None:
                base_body["mock_stall_after"] = args.mock_stall_after
                base_body["mock_stall_secs"] = args.mock_stall_secs
            payload = json.dumps(base_body).encode()

            lock = threading.Lock()
            latencies: list[float] = []
            statuses: list[int] = []
            next_idx = 0
            errors: list[str] = []

            def worker() -> None:
                nonlocal next_idx
                while True:
                    with lock:
                        if next_idx >= args.requests:
                            return
                        next_idx += 1
                    url = f"http://127.0.0.1:{proxy_port}/v1/chat/completions"
                    req = urllib.request.Request(
                        url, data=payload,
                        headers={"Authorization": "Bearer tb-stress",
                                 "Content-Type": "application/json"})
                    start = time.monotonic()
                    try:
                        with urllib.request.urlopen(req, timeout=60) as r:
                            r.read()  # consume the (possibly SSE) body
                        statuses.append(r.status)
                    except urllib.error.HTTPError as e:
                        statuses.append(e.code)
                        e.read()
                    except Exception as e:  # noqa: BLE001
                        errors.append(str(e))
                        statuses.append(0)
                    latencies.append((time.monotonic() - start) * 1000.0)

            start_total = time.monotonic()
            threads = [threading.Thread(target=worker)
                       for _ in range(args.concurrency)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            total_secs = max(time.monotonic() - start_total, 1e-6)

            # Wait for the async accounting thread to land all request_log rows.
            deadline = time.monotonic() + 10.0
            rows = []
            while time.monotonic() < deadline:
                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT duration_ms, upstream_ttft_ms, ttft_ms, status_code "
                    "FROM request_log WHERE model='stress-model' "
                    "ORDER BY id DESC LIMIT ?", (args.requests,)
                ).fetchall()
                conn.close()
                if len(rows) >= min(args.requests, 10):
                    break
                time.sleep(0.05)

            durations = sorted(r[0] or 0 for r in rows)
            up_ttft = sorted(r[1] or 0 for r in rows if r[1] is not None)
            ttft = sorted(r[2] or 0 for r in rows if r[2] is not None)
            ok = statuses.count(200)
            lat_sorted = sorted(latencies)

            def pct(vals: list[float], p: float) -> float:
                return percentile(vals, p)

            mode = "stream" if not args.no_stream else "nonstream"
            print(
                f"RESULT tag={args.tag} mode={mode} "
                f"concurrency={args.concurrency} requests={args.requests} "
                f"ok={ok} failures={len(statuses) - ok} "
                f"throughput_rps={args.requests / total_secs:.1f} "
                f"client_p50={pct(lat_sorted, 50):.1f} "
                f"client_p95={pct(lat_sorted, 95):.1f} "
                f"client_p99={pct(lat_sorted, 99):.1f} "
                f"upstream_ttft_p50={pct(up_ttft, 50):.1f} "
                f"upstream_ttft_p99={pct(up_ttft, 99):.1f} "
                f"duration_p50={pct(durations, 50):.1f} "
                f"duration_p99={pct(durations, 99):.1f} "
                f"errors={errors[:3]}",
                flush=True,
            )
            client_p95 = pct(lat_sorted, 95)
            queue_p95 = 0.0
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{proxy_port}/health", timeout=1) as health_response:
                    health = json.loads(health_response.read().decode())
                queue_p95 = float(health.get("queue", {}).get("p95_ms", 0) or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                if args.max_queue_p95 > 0:
                    print("ERROR: unable to read queue metrics", flush=True)
                    sys.exit(3)
            throughput = args.requests / total_secs
            if args.min_rps > 0 and throughput < args.min_rps:
                print(f"ERROR: throughput {throughput:.1f} < {args.min_rps:.1f} RPS",
                      flush=True)
                sys.exit(3)
            if args.max_p95 > 0 and client_p95 > args.max_p95:
                print(f"ERROR: client p95 {client_p95:.1f} > {args.max_p95:.1f} ms",
                      flush=True)
                sys.exit(3)
            if args.max_queue_p95 > 0 and queue_p95 > args.max_queue_p95:
                print(f"ERROR: queue p95 {queue_p95:.1f} > {args.max_queue_p95:.1f} ms",
                      flush=True)
                sys.exit(3)
            if ok < len(statuses):
                print("WARNING: non-200 responses:", {
                    s: statuses.count(s) for s in set(statuses) if s != 200
                }, flush=True)
                sys.exit(2)
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
    upstream_thread.join(timeout=2)


if __name__ == "__main__":
    main()
