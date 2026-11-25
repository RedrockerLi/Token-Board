#!/usr/bin/env python3
"""Loopback coverage for the Gemini GenerateContent wire format.

Covers both directions of the fourth format: a Gemini client against a Gemini
upstream (passthrough), a Gemini client against an Anthropic upstream, and an
Anthropic client against a Gemini upstream — plus the Gemini catalog shape and
the accounting row.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import stop_process

GEMINI_BODY = {
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": "Hello from Gemini"}]},
        "finishReason": "STOP",
        "index": 0,
    }],
    "usageMetadata": {"promptTokenCount": 12,
                      "cachedContentTokenCount": 4,
                      "candidatesTokenCount": 6,
                      "totalTokenCount": 18},
    "modelVersion": "gemini-2.5-pro-002",
    "responseId": "gem-1",
}

GEMINI_STREAM = [
    {"responseId": "gem-s1", "modelVersion": "gemini-2.5-pro",
     "candidates": [{"content": {"role": "model",
                                 "parts": [{"text": "Hello "}]}, "index": 0}]},
    {"responseId": "gem-s1",
     "candidates": [{"content": {"role": "model",
                                 "parts": [{"text": "from Gemini"}]}, "index": 0}]},
    {"responseId": "gem-s1",
     "candidates": [{"content": {"role": "model", "parts": []},
                     "finishReason": "STOP", "index": 0}],
     "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 6,
                       "totalTokenCount": 18}},
]

ANTHROPIC_BODY = {
    "id": "msg_1", "type": "message", "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "Hello from Claude"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 9, "output_tokens": 5},
}

ANTHROPIC_STREAM = (
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{"
    "\"id\":\"msg_s1\",\"type\":\"message\",\"role\":\"assistant\","
    "\"model\":\"claude-test\",\"content\":[],\"usage\":{"
    "\"input_tokens\":9,\"output_tokens\":1}}}\n\n"
    "event: content_block_start\ndata: {\"type\":\"content_block_start\","
    "\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\","
    "\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"Hello\"}}\n\n"
    "event: content_block_stop\ndata: {\"type\":\"content_block_stop\","
    "\"index\":0}\n\n"
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{"
    "\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":5}}\n\n"
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port: int) -> None:
    for _ in range(150):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=.2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.02)
    raise AssertionError("proxy did not become healthy")


class Recorder:
    """Shared request log for both fake upstreams."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def record(self, handler, body) -> None:
        with self.lock:
            self.requests.append({"path": handler.path,
                                  "headers": dict(handler.headers),
                                  "body": body})

    def take(self) -> dict:
        with self.lock:
            assert self.requests, "upstream received no request"
            return self.requests.pop(0)

    def take_all(self) -> list[dict]:
        with self.lock:
            taken, self.requests = self.requests, []
            return taken


RECORDER = Recorder()


def header(request: dict, name: str) -> str | None:
    for key, value in request["headers"].items():
        if key.lower() == name.lower():
            return value
    return None


class GeminiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        RECORDER.record(self, body)
        if "gemini-429" in self.path:
            payload = json.dumps({"error": {
                "code": 429, "status": "RESOURCE_EXHAUSTED",
                "message": "quota exceeded",
                "details": [{
                    "@type": "type.googleapis.com/google.rpc.RetryInfo",
                    "retryDelay": "16s"}]}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if ":streamGenerateContent" in self.path:
            payload = "".join(f"data: {json.dumps(frame)}\n\n"
                              for frame in GEMINI_STREAM).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(GEMINI_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AnthropicHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        RECORDER.record(self, body)
        if body.get("stream"):
            payload = ANTHROPIC_STREAM.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = json.dumps(ANTHROPIC_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _open(request) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        raise AssertionError(
            f"{request.full_url} -> HTTP {error.code}: "
            f"{error.read().decode(errors='replace')}") from error


def post(url: str, payload: dict, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    return _open(request)


def get(url: str, headers: dict) -> tuple[int, bytes]:
    return _open(urllib.request.Request(url, headers=headers))


def seed(db: Path, schema: Path, root: Path, gemini_port: int,
         anthropic_port: int) -> None:
    sys.path.insert(0, str(root))
    from app.db.schema_upgrade import ensure_local_databases

    ensure_local_databases(str(db), str(db.with_name("dashboard.db")), schema)
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO accounts(id,uuid,name,account_kind) "
                     "VALUES(1,'account-1','google-account','proxy')")
        conn.execute(
            "INSERT INTO upstreams(id,account_id,name,base_url,api_format,"
            "auth_scheme,max_concurrency) "
            "VALUES(1,1,'gemini',?,'google','auto',16)",
            (f"http://127.0.0.1:{gemini_port}/v1beta",))
        conn.execute(
            "INSERT INTO upstreams(id,account_id,name,base_url,api_format,"
            "auth_scheme,max_concurrency) "
            "VALUES(2,1,'anthropic',?,'anthropic','auto',16)",
            (f"http://127.0.0.1:{anthropic_port}",))
        conn.execute(
            "INSERT INTO upstream_model_catalog(upstream_id,model_id) "
            "VALUES(1,'gemini-2.5-pro')")
        conn.execute("INSERT INTO route_sets(id,uuid,account_id,name) "
                     "VALUES(1,'route-1',1,'google')")
        conn.execute(
            "INSERT INTO route_rules(route_set_id,model_pattern,priority,"
            "upstream_id) VALUES(1,'*',0,1)")
        conn.execute("INSERT INTO route_sets(id,uuid,account_id,name) "
                     "VALUES(2,'route-2',1,'anthropic')")
        conn.execute(
            "INSERT INTO route_rules(route_set_id,model_pattern,priority,"
            "upstream_id) VALUES(2,'*',0,2)")
        conn.execute(
            "INSERT INTO client_keys(id,uuid,key_value,label,route_set_id) "
            "VALUES(1,'client-1','tb-google','google',1)")
        conn.execute(
            "INSERT INTO client_keys(id,uuid,key_value,label,route_set_id) "
            "VALUES(2,'client-2','tb-anthropic','anthropic',2)")
        for credential, upstream in ((1, 1), (2, 2)):
            conn.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,"
                "position,key_masked,enabled) VALUES(?,?,?,0,'sk-…mock',1)",
                (f"credential-{credential}", credential, upstream))
            conn.execute(
                "INSERT INTO upstream_secrets(credential_uuid,secret_value) "
                "VALUES(?,?)", (f"credential-{credential}",
                                f"sk-upstream-{upstream}"))
        conn.execute(
            "INSERT INTO billing_contracts(uuid,account_id,charge_type,"
            "billing_scope,valid_from) "
            "VALUES('contract-1',1,'metered','account','2020-01-01')")
        conn.commit()
    finally:
        conn.close()


def check_gemini_passthrough(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = post(
        f"http://127.0.0.1:{proxy_port}/v1beta/models/gemini-2.5-pro:generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
         "generationConfig": {"maxOutputTokens": 16}},
        {"x-goog-api-key": "tb-google"})
    assert status == 200, status
    parsed = json.loads(body)
    assert parsed["candidates"][0]["content"]["parts"][0]["text"] == \
        "Hello from Gemini", parsed

    upstream = RECORDER.take()
    assert upstream["path"] == \
        "/v1beta/models/gemini-2.5-pro:generateContent", upstream["path"]
    assert header(upstream, "x-goog-api-key") == "sk-upstream-1", \
        upstream["headers"]
    assert header(upstream, "Authorization") is None
    # The model is a path variable on this wire and must not appear in the body.
    assert "model" not in upstream["body"], upstream["body"]
    assert upstream["body"]["contents"][0]["parts"][0]["text"] == "ping"


def check_gemini_stream_passthrough(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = post(
        f"http://127.0.0.1:{proxy_port}/v1beta/models/gemini-2.5-pro:streamGenerateContent",
        {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
        {"x-goog-api-key": "tb-google"})
    assert status == 200, status
    text = body.decode()
    assert "data:" in text, text
    assert "from Gemini" in text, text
    upstream = RECORDER.take()
    assert upstream["path"].endswith(
        "/models/gemini-2.5-pro:streamGenerateContent?alt=sse"), upstream["path"]


def check_anthropic_client_to_gemini(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = post(
        f"http://127.0.0.1:{proxy_port}/v1/messages",
        {"model": "gemini-2.5-pro", "max_tokens": 64,
         "system": "You are helpful.",
         "messages": [{"role": "user", "content": "hi"}]},
        {"Authorization": "Bearer tb-google"})
    assert status == 200, status
    parsed = json.loads(body)
    assert parsed["content"][0]["text"] == "Hello from Gemini", parsed
    assert parsed["stop_reason"] == "end_turn", parsed
    # Anthropic's wire reports a net input bucket, so the cache read is
    # subtracted from Gemini's cache-inclusive prompt (12 - 4).
    assert parsed["usage"]["input_tokens"] == 8, parsed["usage"]
    assert parsed["usage"]["cache_read_input_tokens"] == 4, parsed["usage"]

    upstream = RECORDER.take()
    assert upstream["path"] == "/v1beta/models/gemini-2.5-pro:generateContent", \
        upstream["path"]
    assert header(upstream, "x-goog-api-key") == "sk-upstream-1"
    assert "model" not in upstream["body"], upstream["body"]
    assert upstream["body"]["systemInstruction"]["parts"][0]["text"] == \
        "You are helpful.", upstream["body"]
    assert upstream["body"]["contents"][0]["role"] == "user"


def check_gemini_client_to_anthropic(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = post(
        f"http://127.0.0.1:{proxy_port}/v1beta/models/gemini-2.5-pro:generateContent",
        {"systemInstruction": {"parts": [{"text": "Be brief."}]},
         "contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        {"x-goog-api-key": "tb-anthropic"})
    assert status == 200, status
    parsed = json.loads(body)
    assert parsed["candidates"][0]["content"]["parts"][0]["text"] == \
        "Hello from Claude", parsed
    assert parsed["candidates"][0]["finishReason"] == "STOP", parsed
    assert parsed["usageMetadata"]["promptTokenCount"] == 9, parsed
    assert parsed["modelVersion"] == "claude-test", parsed

    upstream = RECORDER.take()
    assert upstream["path"] == "/v1/messages", upstream["path"]
    assert header(upstream, "x-api-key") == "sk-upstream-2", upstream["headers"]
    assert upstream["body"]["model"] == "gemini-2.5-pro", upstream["body"]
    assert upstream["body"]["stream"] is False, upstream["body"]
    assert upstream["body"]["system"] == "Be brief.", upstream["body"]


def check_gemini_stream_client_to_anthropic(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = post(
        f"http://127.0.0.1:{proxy_port}/v1beta/models/gemini-2.5-pro:streamGenerateContent",
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        {"x-goog-api-key": "tb-anthropic"})
    assert status == 200, status
    text = body.decode()
    assert "data:" in text, text
    fragments = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        frame = json.loads(line[len("data:"):].strip())
        for candidate in frame.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                fragments.append(part.get("text", ""))
    assert "".join(fragments) == "Hello", fragments
    upstream = RECORDER.take()
    assert upstream["body"]["stream"] is True, upstream["body"]


def check_retry_hint(proxy_port: int) -> None:
    # A converted error carries the provider's own retry hint; a same-format
    # error keeps the upstream body verbatim instead.
    RECORDER.take_all()
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/messages",
        data=json.dumps({"model": "gemini-429", "max_tokens": 8,
                         "messages": [{"role": "user",
                                       "content": "hi"}]}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer tb-google"})
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 429, error.code
        # google.rpc.RetryInfo.retryDelay survives as the client's hint.
        assert error.headers.get("Retry-After") == "16", dict(error.headers)
        payload = json.loads(error.read())
        assert payload["error"]["type"] == "resource_exhausted", payload
    else:
        raise AssertionError("the upstream 429 was not surfaced")


def check_gemini_catalog(proxy_port: int) -> None:
    RECORDER.take_all()
    status, body = get(f"http://127.0.0.1:{proxy_port}/v1beta/models",
                       {"x-goog-api-key": "tb-google"})
    assert status == 200, status
    parsed = json.loads(body)
    assert parsed["models"][0]["name"] == "models/gemini-2.5-pro", parsed
    assert "generateContent" in \
        parsed["models"][0]["supportedGenerationMethods"], parsed
    # The catalog is a local read; no upstream call was made.
    assert RECORDER.take_all() == []


def check_accounting(db: Path) -> None:
    deadline = time.monotonic() + 5
    row = None
    while time.monotonic() < deadline:
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT model,status_code,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens FROM request_log "
                "ORDER BY id ASC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row is not None:
            break
        time.sleep(.05)
    assert row is not None, "no request_log row was written"
    model, status, prompt, completion, cache_read, total = row
    assert status == 200, row
    # Gemini's prompt count already includes the cache read.
    assert (prompt, completion, cache_read, total) == (12, 6, 4, 18), row


def main() -> None:
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    root = Path(sys.argv[3]).resolve()

    gemini_port, anthropic_port = free_port(), free_port()
    gemini = ThreadingHTTPServer(("127.0.0.1", gemini_port), GeminiHandler)
    anthropic = ThreadingHTTPServer(("127.0.0.1", anthropic_port),
                                    AnthropicHandler)
    for server in (gemini, anthropic):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "token-board.db"
        seed(db, schema, root, gemini_port, anthropic_port)
        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(binary), "--db", str(db), "--host", "127.0.0.1",
             "--port", str(proxy_port), "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_health(proxy_port)
            check_gemini_passthrough(proxy_port)
            check_gemini_stream_passthrough(proxy_port)
            check_anthropic_client_to_gemini(proxy_port)
            check_gemini_client_to_anthropic(proxy_port)
            check_gemini_stream_client_to_anthropic(proxy_port)
            check_retry_hint(proxy_port)
            check_gemini_catalog(proxy_port)
            check_accounting(db)
        finally:
            diagnostics = stop_process(proxy, check_returncode=False)
            for server in (gemini, anthropic):
                server.shutdown()
            if proxy.returncode not in (0, -15):
                raise AssertionError(diagnostics)


if __name__ == "__main__":
    main()
