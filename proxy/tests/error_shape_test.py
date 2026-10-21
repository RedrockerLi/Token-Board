#!/usr/bin/env python3
"""Lock the unified terminal-error rendering across chat and embeddings.

/v1/models no longer forwards upstream: it serves the dashboard-maintained
catalog purely locally (Anthropic clients get an empty {"models":[]}), and
cases 8-9 pin that contract.

Regression gate for the C audit finding: the non-streaming handlers and
the streaming tail used to render failure bodies three different ways (chat via
harness_codec.serialize_error_body + parse_error_body, embeddings via
json_error with a raw "Upstream error: ..." text).  After the refactor every
forwarding endpoint routes through the shared render_terminal_error(), so the
error JSON shape is uniform:

  * all provider-quota-cooled 429 -> {"error": {"message", "type":"rate_limit_error", "code":429}}
  * local concurrency capacity -> 503 + Retry-After: 1
  * upstream timeout    -> 504 + Connection: close + {"error":{...,"type":"timeout_error","code":504}}
  * used upstream error -> the structured parse_error_body output (not a
                           synthetic "Upstream error: <raw>" text)
  * Anthropic clients   -> the {"type":"error","error":{...}} envelope

The busy-429 branches need zero attempts.  Because AccountGate treats
max_concurrency<=0 as unlimited, the busy state is reached the proven way
(fallback_matrix A3): a recurring key that answered 429 with the real
GoUsageLimitError envelope enters the 5h cooldown; the next request then finds
every candidate cooling and never contacts the upstream.

Usage:
  python3 error_shape_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
import http.client
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
from pathlib import Path

from http.server import ThreadingHTTPServer

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


def send(proxy_port: int, method: str, path: str, local_key: str,
         body: dict | None, timeout: float = 20,
         extra_headers: dict | None = None) -> tuple[int, dict, object]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {local_key}",
               "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}{path}",
        data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        parsed = json.loads(raw) if raw else {}
        return e.code, parsed, e.headers


def send_stream(proxy_port: int, path: str, local_key: str,
                body: dict, timeout: float = 20) -> tuple[int, bytes, object]:
    """Send a streaming request while keeping the downstream connection open."""
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=timeout)
    try:
        conn.request(
            "POST", path, body=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {local_key}",
                     "Content-Type": "application/json",
                     "Connection": "keep-alive"})
        response = conn.getresponse()
        return response.status, response.read(), response.headers
    finally:
        conn.close()


def ctrl(upstream_port: int, payload: dict) -> None:
    """Toggle the mock's out-of-band per-key status map (/__ctrl)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{upstream_port}/__ctrl",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def ctrl_status(upstream_port: int) -> dict:
    """The mock's probe counters and status map (/__ctrl/status)."""
    with urllib.request.urlopen(
            f"http://127.0.0.1:{upstream_port}/__ctrl/status",
            timeout=5) as r:
        return json.loads(r.read().decode())


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
    from scripts.mock_upstream import Handler
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
            from v1_fixture import add_plain_route, add_upstream

            def plain(key: str, name: str, *,
                      recurring: bool = False, api_format: str = "openai",
                      max_concurrency: int = 64):
                account_id, up, _ = add_upstream(
                    conn, name, base_url, [key], recurring=recurring,
                    api_format=api_format, max_concurrency=max_concurrency)
                add_plain_route(conn, f"tb-{name}", name, up, account_id,
                                model_pattern="*")
                return account_id, up

            # Separate accounts so the cooldown state of one scenario can never
            # leak into another.
            plain("sk-chat-500", "chat500")
            plain("sk-chat-400", "chat400")
            plain("sk-chat-403", "chat403")
            plain("sk-chat-to", "chatto")
            plain("sk-chat-stream", "chatstream")
            plain("sk-chat-cap", "chatcap", max_concurrency=1)
            plain("sk-chat-busy", "chatbusy", recurring=True)
            plain("sk-anth-busy", "anthbusy", recurring=True, api_format="anthropic")
            plain("sk-emb-500", "emb500")
            plain("sk-emb-busy", "embbusy", recurring=True)
            _, mod500_up = plain("sk-mod-500", "mod500")
            _, modbusy_up = plain("sk-mod-busy", "modbusy")
            # /v1/models serves the stored catalog; §8-9 pin exactly that.
            conn.executemany(
                "INSERT INTO upstream_model_catalog(upstream_id, model_id) "
                "VALUES(?,?)",
                [(modbusy_up, "modbusy-catalog-a"),
                 (modbusy_up, "modbusy-catalog-b"),
                 (mod500_up, "mod500-catalog-a")])

            # Fast timeout round-trip for the chat timeout case.
            conn.execute(
                "UPDATE proxy_timeout_config SET non_streaming_timeout=1 "
                "WHERE endpoint_kind='chat'")
            conn.execute(
                "UPDATE proxy_timeout_config SET streaming_first_byte_timeout=1 "
                "WHERE endpoint_kind='chat'")
            conn.execute(
                "UPDATE upstreams SET base_url=?", (base_url,))
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
            chat = {"messages": [{"role": "user", "content": "hi"}]}

            # Cross-format requests with an unknown Anthropic content block
            # must expose the preflight feature detail both in the response
            # and in the structured proxy log.  The detail is metadata only;
            # the sentinel must never be copied into the journal.
            status, body, _ = send(
                proxy_port, "POST", "/v1/messages", "tb-chatto",
                {"model": "chatto-model", "max_tokens": 16,
                 "messages": [{"role": "user", "content": [{
                     "type": "future_block",
                     "sentinel": "must-not-appear-in-log"}]}]})
            assert status == 422, (status, body)
            assert body["error"]["code"] == "unsupported_feature", body
            assert body["error"]["details"] == [{
                "feature": "unknown_content",
                "target": "openai",
                "reason": "unknown content blocks cannot be converted losslessly",
            }], body
            print("0a OK: unsupported feature includes safe preflight detail")

            # A route miss is rejected by cpp-httplib before a business
            # handler runs; the framework logger must still emit a structured
            # WARN without any request credential material.
            status, _, _ = send(proxy_port, "GET", "/not-found", "tb-chat500", None)
            assert status == 404, status
            print("0 OK: framework route miss is logged")

            # ── 1. Chat OpenAI used-failure 500 → raw passthrough ──
            upstream_marker = "marker-upstream-503-body"
            status, body, _ = send(proxy_port, "POST", "/v1/chat/completions",
                "tb-chat500", {**chat, "model": "chat500-model",
                               "mock_status_by_key": {"sk-chat-500": 500},
                               "mock_error_body": {
                                   "error": {"message": upstream_marker,
                                             "type": "mock_error",
                                             "code": 500}}})
            assert status == 500, f"chat 500 status {status}"
            assert body == {"error": {"message": upstream_marker,
                                      "type": "mock_error", "code": 500}}, body
            print(f"1 OK: chat OpenAI used-failure 500 passthrough -> {body}")

            # ── 1b. Request-level (400) and auth-level (403) upstream error
            #        bodies are journaled exactly like 5xx bodies, so the
            #        operator can see the reason the upstream gave. ──
            for status_code, local_key, marker in (
                    (400, "tb-chat400", "marker-upstream-400-body"),
                    (403, "tb-chat403", "marker-upstream-403-body")):
                status, body, _ = send(proxy_port, "POST",
                    "/v1/chat/completions", local_key,
                    {**chat, "model": f"chat{status_code}-model",
                     "mock_status": status_code,
                     "mock_error_body": {
                         "error": {"message": marker,
                                   "type": "mock_error",
                                   "code": status_code}},
                     "request_marker": "marker-upstream-request-body",
                     "request_padding": "x" * 600 +
                                        "marker-request-tail-must-not-appear"})
                assert status == status_code, (status, body)
                assert body == {"error": {"message": marker,
                                          "type": "mock_error",
                                          "code": status_code}}, body
                print(f"1b OK: upstream {status_code} body passthrough -> {body}")

            # ── 2. Chat used-failure timeout → 504 + Connection: close ──
            status, body, headers = send(proxy_port, "POST",
                "/v1/chat/completions", "tb-chatto",
                {**chat, "model": "chatto-model", "mock_delay": 3})
            assert status == 504, f"chat timeout status {status}"
            assert headers.get("Connection") == "close", headers.get("Connection")
            err = body["error"]
            assert err["type"] == "timeout_error" and err["code"] == 504, body
            assert str(err["message"]).lower().startswith("upstream timeout"), body
            print(f"2 OK: chat timeout -> 504 + Connection: close + {err['type']}")

            # ── 3. Streaming first-byte timeout → 504 + Connection: close ──
            status, raw, headers = send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatstream",
                {**chat, "model": "chatstream-model", "stream": True,
                 "mock_delay": 3}, timeout=10)
            assert status == 504, (
                f"stream timeout status {status}: headers={headers}, raw={raw!r}")
            assert headers.get("Connection") == "close", headers
            assert headers.get("Keep-Alive") is None, headers
            assert b"timeout_error" in raw, raw
            print("3 OK: streaming timeout -> 504 + Connection: close")

            # A proxy-generated 504 is a provider fault: the key takes a
            # short avoid so the immediate retry cannot hammer the upstream
            # that just failed to answer, and the client gets a fast,
            # explicit 503 instead.
            status, body, headers = send(
                proxy_port, "POST", "/v1/chat/completions", "tb-chatstream",
                {**chat, "model": "chatstream-model", "stream": False})
            assert status == 503, f"post-timeout request: {status} {body}"
            retry_after = headers.get("Retry-After")
            assert retry_after is not None and 20 <= int(retry_after) <= 30, \
                headers
            assert body["error"]["type"] == "service_unavailable", body
            assert body["error"]["message"] == \
                "All upstream keys are temporarily unavailable", body
            print("3b OK: proxy timeout -> provider-fault avoid with the real "
                  f"Retry-After ({retry_after}s), not a 1s re-hammer")

            # Local per-key capacity is a proxy resource condition, not an
            # upstream rate limit: it returns 503 while the slot is occupied.
            first_done = threading.Event()
            def hold_capacity():
                try:
                    # Within the 1s non-streaming timeout: the holder must
                    # succeed (a timeout would fault the key and turn the
                    # follow-up below into a fast 503 instead).
                    send(proxy_port, "POST", "/v1/chat/completions", "tb-chatcap",
                         {**chat, "model": "chatcap-model", "mock_delay": 0.6},
                         timeout=10)
                finally:
                    first_done.set()
            first = threading.Thread(target=hold_capacity)
            first.start()
            time.sleep(0.15)
            status, body, headers = send(
                proxy_port, "POST", "/v1/chat/completions", "tb-chatcap",
                {**chat, "model": "chatcap-model"})
            assert status == 503, (status, body, headers)
            assert headers.get("Retry-After") == "1", headers
            assert body["error"]["code"] == 503, body
            assert body["error"]["type"] == "service_unavailable", body
            first.join(timeout=10)
            assert first_done.is_set(), "capacity holder did not finish"
            status, body, _ = send(
                proxy_port, "POST", "/v1/chat/completions", "tb-chatcap",
                {**chat, "model": "chatcap-model"})
            assert status == 200, (status, body)
            print("3c OK: local key capacity -> 503 + Retry-After, then releases")

            # ── 4. Streaming all-candidates-busy → 429 ──
            send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatbusy",
                {**chat, "model": "chatbusy-model", "stream": True,
                 "mock_status": 429, "mock_error_type": "GoUsageLimitError"},
                timeout=10)
            status, raw, headers = send_stream(
                proxy_port, "/v1/chat/completions", "tb-chatbusy",
                {**chat, "model": "chatbusy-model", "stream": True},
                timeout=10)
            assert status == 429, (
                f"stream busy status {status}: headers={headers}, raw={raw!r}")
            assert b"rate_limit_error" in raw, raw
            print("4 OK: streaming all-candidates-busy -> 429")

            # ── 5. Anthropic chat busy-429 → Anthropic envelope ──
            # Req1: a recurring key answers 429 GoUsageLimitError → 5h cooldown.
            send(proxy_port, "POST", "/v1/messages", "tb-anthbusy",
                 {"model": "anthbusy-model", "messages": chat["messages"],
                  "mock_status": 429, "mock_error_type": "GoUsageLimitError"})
            status, body, _ = send(proxy_port, "POST", "/v1/messages",
                "tb-anthbusy", {"model": "anthbusy-model",
                                "messages": chat["messages"]})
            assert status == 429, f"anth busy status {status}"
            assert body == {"type": "error", "error": {
                "message": "All upstream keys are in provider quota cooldown",
                "type": "rate_limit_error", "code": 429}}, body
            print(f"5 OK: Anthropic chat busy-429 envelope -> {body}")

            # ── 6. Embeddings used-failure 500 → structured, no "Upstream error:" ──
            status, body, _ = send(proxy_port, "POST", "/v1/embeddings",
                "tb-emb500", {"model": "emb500-model", "input": "hi",
                              "mock_status_by_key": {"sk-emb-500": 500}})
            assert status == 500, f"emb 500 status {status}"
            assert body == {"error": {"message": "mock error",
                                      "type": "mock_error", "code": 500}}, body
            assert "Upstream error" not in json.dumps(body)
            print(f"6 OK: embeddings used-failure 500 structured -> {body}")

            # ── 7. Embeddings busy-429 (recurring key cooled) ──
            send(proxy_port, "POST", "/v1/embeddings", "tb-embbusy",
                 {"model": "embbusy-model", "input": "hi",
                  "mock_status": 429, "mock_error_type": "GoUsageLimitError"})
            status, body, _ = send(proxy_port, "POST", "/v1/embeddings",
                "tb-embbusy", {"model": "embbusy-model", "input": "hi"})
            assert status == 429, f"emb busy status {status}"
            assert body == {"error": {"message":
                "All upstream keys are in provider quota cooldown",
                "type": "rate_limit_error", "code": 429}}, body
            print(f"7 OK: embeddings busy-429 -> {body}")

            # ── 8. Models is a local catalog read: the stored catalog is
            #       served, the upstream is never contacted (the pinned 429
            #       would surface if it were) and cooling never matters ──
            ctrl(upstream_port, {"set": {"sk-mod-busy": 429}})
            probes_before = ctrl_status(upstream_port)["probes"].get(
                "sk-mod-busy", 0)
            status, body, _ = send(proxy_port, "GET", "/v1/models",
                "tb-modbusy", None)
            probes_after = ctrl_status(upstream_port)["probes"].get(
                "sk-mod-busy", 0)
            ctrl(upstream_port, {"clear": ["sk-mod-busy"]})
            assert status == 200, status
            assert [m["id"] for m in body["data"]] == \
                ["modbusy-catalog-a", "modbusy-catalog-b"], body
            assert probes_after == probes_before, \
                "models must not contact the upstream"
            print("8 OK: models served from the stored catalog")

            # ── 9. Anthropic clients (cc) get the empty catalog: any model
            #       name validates client-side, still a pure local read ──
            status, body, _ = send(proxy_port, "GET", "/v1/models",
                "tb-mod500", None,
                extra_headers={"anthropic-version": "2023-06-01"})
            assert status == 200 and body == {"models": []}, (status, body)
            probes = ctrl_status(upstream_port)["probes"].get("sk-mod-500", 0)
            assert probes == 0, "the anthropic catalog must be local"
            print("9 OK: anthropic clients get the empty local catalog")

            print("error_shape tests passed")
        finally:
            stderr_output = stop_process(proxy)
            if proxy.returncode in (0, -15):
                assert "[ProxyError]" in stderr_output, stderr_output
                assert "phase=upstream_attempt" in stderr_output, stderr_output
                assert "phase=request_final" in stderr_output, stderr_output
                assert "phase=candidate_selection" in stderr_output, stderr_output
                assert "reason=upstream_timeout" in stderr_output, stderr_output
                assert "reason=provider_quota_cooldown" in stderr_output, stderr_output
                assert "reason=route_not_found" in stderr_output, stderr_output
                assert (
                    "validation_details=unknown_content@openai:"
                    "unknown_content_blocks_cannot_be_converted_losslessly"
                    in stderr_output
                ), stderr_output
                # A real upstream error body is captured, truncated and
                # single-line for every >=400 response, so the next incident
                # can tell a request-shape 400 from capacity, gateway,
                # account or credential failures.
                assert "upstream_body=" in stderr_output, stderr_output
                assert "marker-upstream-503-body" in stderr_output, stderr_output
                assert "marker-upstream-400-body" in stderr_output, stderr_output
                assert "marker-upstream-403-body" in stderr_output, stderr_output
                # The rejected attempt carries the request body that was sent,
                # field by field, so the next incident can pair request shape
                # with response.  Each field is capped at 512 bytes, so the
                # padding field's tail must not reach the journal.
                assert "upstream_request_model=" in stderr_output, stderr_output
                assert "upstream_request_request_marker=" in stderr_output, \
                    stderr_output
                assert "marker-upstream-request-body" in stderr_output, \
                    stderr_output
                assert "marker-request-tail-must-not-appear" not in \
                    stderr_output, stderr_output
                # The terminal log records the Retry-After the client saw.
                assert "retry_after_seconds=" in stderr_output, stderr_output
                # Request credentials must never be copied into the journal
                # record.  The failed-attempt request body is intentionally
                # journaled, so this only asserts that the downstream client
                # credential (an Authorization header) never leaks; the mock's
                # own key names appear because the test payloads carry them to
                # the mock, not because the proxy logs a header.
                assert "tb-chat500" not in stderr_output, stderr_output
                assert "must-not-appear-in-log" not in stderr_output, stderr_output
            if proxy.returncode not in (0, -15):
                print(stderr_output, file=sys.stderr)

    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
