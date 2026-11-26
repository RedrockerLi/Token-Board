#!/usr/bin/env python3
"""Verify the 32 MiB request-body cap on every HTTP framing.

The cap comes from ``Server::set_payload_max_length`` (proxy/src/main.cpp).
In the vendored cpp-httplib only the ``Content-Length`` branch of
``detail::read_content`` compared against it, so ``Transfer-Encoding:
chunked`` bodies and bodies without any length header were read without a
bound (audit 04-C-14 / 05-TB05-F01).  These cases pin all three framings plus
the memory consequence of an unbounded read.
"""

from __future__ import annotations

import json
import os
import socket
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

MIB = 1024 * 1024
BODY_LIMIT = 32 * MIB
# Peak-RSS growth allowed per request.  A request may buffer at most
# BODY_LIMIT of body and a growing std::string can transiently hold the old
# and the new buffer, so 2x the cap is the honest bound.  An unbounded read
# instead grows by roughly the body size (04-C-14 measured 11.7 -> 143 MB for
# a single 128 MiB body), which this threshold separates clearly.
HWM_GROWTH_LIMIT = 2 * BODY_LIMIT


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
            time.sleep(0.05)
    raise AssertionError("proxy did not become healthy")


def json_body(total: int) -> bytes:
    """A valid chat-completions JSON body of exactly ``total`` bytes."""
    prefix = b'{"model":"tb-payload","stream":false,"pad":"'
    suffix = b'","messages":[{"role":"user","content":"ok"}]}'
    pad = total - len(prefix) - len(suffix)
    assert pad >= 0, total
    return prefix + b"a" * pad + suffix


def hwm_bytes(pid: int) -> int:
    """Peak resident set size of a process, from /proc/<pid>/status."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    raise AssertionError(f"VmHWM missing for pid {pid}")


def request_head(length: int | None, *, chunked: bool) -> bytes:
    headers = [b"POST /v1/chat/completions HTTP/1.1",
               b"Host: 127.0.0.1",
               b"Authorization: Bearer tb-payload",
               b"Content-Type: application/json"]
    if chunked:
        headers.append(b"Transfer-Encoding: chunked")
    elif length is not None:
        headers.append(b"Content-Length: " + str(length).encode())
    return b"\r\n".join(headers) + b"\r\n\r\n"


def plain_frames(body: bytes, piece: int = MIB):
    for start in range(0, len(body), piece):
        yield body[start:start + piece]


def chunked_frames(body: bytes, piece: int = MIB):
    for start in range(0, len(body), piece):
        data = body[start:start + piece]
        yield b"%x\r\n" % len(data) + data + b"\r\n"
    yield b"0\r\n\r\n"


class RawResponse:
    """Status line plus whatever head bytes arrived from a raw request."""

    def __init__(self, status: int | None, head: bytes,
                 write_error: OSError | None) -> None:
        self.status = status
        self.head = head
        self.write_error = write_error

    def describe(self) -> str:
        return f"status={self.status} head={self.head[:60]!r}"


def exchange(port: int, head: bytes, frames, *, half_close: bool,
             read_timeout: float = 30.0) -> RawResponse:
    """Send a hand-written request body and read the response head.

    The body goes out on a helper thread so a server that stops reading (the
    fix aborts the upload as soon as the cap is exceeded) is observed while
    the response arrives.  A write error after that is expected: the server
    closes the connection with unread body bytes still in flight.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    write_error: list[OSError] = []
    writer: threading.Thread | None = None
    try:
        sock.sendall(head)

        def write_body() -> None:
            try:
                for frame in frames:
                    sock.sendall(frame)
                if half_close:
                    sock.shutdown(socket.SHUT_WR)
            except OSError as error:
                write_error.append(error)

        writer = threading.Thread(target=write_body, daemon=True)
        writer.start()
        deadline = time.monotonic() + read_timeout
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 64 * 1024:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return RawResponse(_status_of(data), data,
                           write_error[0] if write_error else None)
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        if writer is not None:
            writer.join(timeout=5)


def _status_of(data: bytes) -> int | None:
    head = data.split(b"\r\n", 1)[0]
    if not head.startswith(b"HTTP/1."):
        return None
    parts = head.split()
    return int(parts[1]) if len(parts) > 1 else None


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    project = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project))
    from scripts.mock_upstream import Handler as MockHandler
    from v1_fixture import add_plain_route, add_upstream, ensure_v2_database

    results: list[tuple[str, str | None]] = []

    def record(label: str, ok: bool, detail: str = "") -> None:
        results.append((label, None if ok else detail))
        print(f"{'ok  ' if ok else 'FAIL'} {label}"
              f"{(' — ' + detail) if detail else ''}")

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), MockHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = None
    try:
        with tempfile.TemporaryDirectory(prefix="token-board-payload-") as raw:
            db = Path(raw) / "token-board.db"
            ensure_v2_database(db, schema)
            with sqlite_connection(db) as conn:
                account, upstream_id, _ = add_upstream(
                    conn, "payload", f"http://127.0.0.1:{upstream_port}",
                    ["sk-payload"], max_concurrency=1)
                add_plain_route(conn, "tb-payload", "payload-route",
                                upstream_id, account)
                conn.commit()

            proxy_port = free_port()
            proxy = subprocess.Popen(
                [str(binary), "--db", str(db), "--host", "127.0.0.1",
                 "--port", str(proxy_port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, env=os.environ.copy(),
            )
            wait_for_health(proxy_port)

            # Content-Length over the cap: the branch that always worked.
            response = exchange(
                proxy_port, request_head(40 * MIB, chunked=False),
                plain_frames(json_body(40 * MIB)), half_close=False)
            record("content_length_40MiB_413", response.status == 413,
                   response.describe())

            # Chunked over the cap: read in full before the fix (audit
            # 05-TB05-F01 saw 200 end-to-end with a valid key).
            response = exchange(
                proxy_port, request_head(None, chunked=True),
                chunked_frames(json_body(40 * MIB)), half_close=False)
            record("chunked_40MiB_413", response.status == 413,
                   response.describe())

            # Exactly at the cap: must still pass, both framings.
            response = exchange(
                proxy_port, request_head(BODY_LIMIT, chunked=False),
                plain_frames(json_body(BODY_LIMIT)), half_close=False)
            record("content_length_exactly_32MiB_200", response.status == 200,
                   response.describe())
            response = exchange(
                proxy_port, request_head(None, chunked=True),
                chunked_frames(json_body(BODY_LIMIT)), half_close=False)
            record("chunked_exactly_32MiB_200", response.status == 200,
                   response.describe())

            # One byte over the cap: must be rejected, both framings.
            response = exchange(
                proxy_port, request_head(BODY_LIMIT + 1, chunked=False),
                plain_frames(json_body(BODY_LIMIT + 1)), half_close=False)
            record("content_length_32MiB_plus_1_413", response.status == 413,
                   response.describe())
            response = exchange(
                proxy_port, request_head(None, chunked=True),
                chunked_frames(json_body(BODY_LIMIT + 1)), half_close=False)
            record("chunked_32MiB_plus_1_413", response.status == 413,
                   response.describe())

            # No Content-Length and no Transfer-Encoding: EOF-delimited body.
            # Before the fix the body was read to EOF and the client then got
            # no response at all (04-C-14 measured response bytes b'').
            response = exchange(
                proxy_port, request_head(None, chunked=False),
                plain_frames(json_body(40 * MIB)), half_close=True)
            record("no_length_eof_413", response.status == 413,
                   response.describe())

            # Without a half-close the old code kept waiting for EOF (only
            # the 5 s socket read timeout ended it) while holding the whole
            # body — 04-C-14 measured b'' plus RSS 11.7 -> 143 MB.  The cap
            # must answer as soon as it is exceeded, without an EOF.
            response = exchange(
                proxy_port, request_head(None, chunked=False),
                plain_frames(json_body(40 * MIB)), half_close=False,
                read_timeout=8)
            record("no_length_without_eof_413", response.status == 413,
                   response.describe() if response.head
                   else "no response (b'')")

            # Memory: two sequential chunked uploads in one proxy process.
            # The body is opaque on purpose: the cap must bite before any
            # JSON parsing, and the point is the read buffer, not the model.
            hwm_before = hwm_bytes(proxy.pid)
            response = exchange(
                proxy_port, request_head(None, chunked=True),
                chunked_frames(b"a" * (BODY_LIMIT + 1)), half_close=False)
            record("VmHWM_chunked_32MiB_plus_1_413", response.status == 413,
                   response.describe())
            hwm_mid = hwm_bytes(proxy.pid)
            response = exchange(
                proxy_port, request_head(None, chunked=True),
                chunked_frames(b"a" * (128 * MIB)), half_close=False)
            record("VmHWM_chunked_128MiB_413", response.status == 413,
                   response.describe())
            hwm_after = hwm_bytes(proxy.pid)

            grow_small = hwm_mid - hwm_before
            grow_large = hwm_after - hwm_mid
            print(f"VmHWM growth: {grow_small / MIB:.1f} MiB after a "
                  f"32 MiB+1 body, +{grow_large / MIB:.1f} MiB after a "
                  f"128 MiB body (limit {HWM_GROWTH_LIMIT / MIB:.0f} MiB)")
            record("VmHWM_bounded_for_32MiB_plus_1",
                   grow_small <= HWM_GROWTH_LIMIT,
                   f"grew {grow_small / MIB:.1f} MiB for a 32 MiB+1 body")
            record("VmHWM_bounded_for_128MiB",
                   grow_large <= HWM_GROWTH_LIMIT,
                   f"grew {grow_large / MIB:.1f} MiB for a 128 MiB body")

            # A rejected request must not damage the service.
            valid = json.dumps({
                "model": "after-payload-limit",
                "stream": False,
                "messages": [{"role": "user", "content": "ok"}],
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                data=valid,
                headers={"Authorization": "Bearer tb-payload",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                record("health_after_rejections", response.status == 200,
                       f"status={response.status}")
                response.read()
    finally:
        if proxy is not None:
            stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)

    failures = [f"{label}: {detail}" for label, detail in results
                if detail is not None]
    if failures:
        raise AssertionError(
            f"{len(failures)} of {len(results)} payload-limit case(s) failed: "
            + "; ".join(failures))
    print(f"payload limit passed ({len(results)} cases)")


if __name__ == "__main__":
    main()
