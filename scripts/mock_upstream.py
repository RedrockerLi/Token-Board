#!/usr/bin/env python3
"""Mock upstream AI API server for testing the proxy.

Speaks all three wire formats (OpenAI chat completions, OpenAI Responses,
Anthropic Messages).  Logs every request (path, headers, body) to
`request_log.txt` next to this script so tests can assert on what the proxy
actually sent upstream.

Behaviour models the real opencode.ai "Console Go" upstream (verified against
https://opencode.ai/zen/go/v1 on 2026-08):

- The OpenAI *chat completions* endpoint enforces three strict rules and
  returns HTTP 400 with the real upstream error body when violated:
    1. `role:"developer"` messages are rejected (only system/user/assistant/
       tool are accepted).
    2. Tools with a non-`function` type (custom/namespace/web_search/...) are
       rejected — they are a Responses-format concept, not representable in
       chat completions.
    3. A `function` tool whose `parameters` is present but is not a JSON
       Schema with `"type":"object"` (e.g. `{}`, `null`, or a bare
       `"type":"string"`) is rejected.
  These rules are format-specific: the Responses/Messages endpoints stay
  lenient (developer/tool types are native there).

- Streaming for the chat completions endpoint mimics the real DeepSeek-family
  backend: a role-start chunk, several `reasoning_content` deltas, then
  `content` deltas, a `finish_reason:"stop"` chunk, an opencode-specific
  `x-opencode-type: inference-cost` usage frame, then `[DONE]`.

Response selection:
  - By default the format is inferred from the URL path
    (/v1/chat/completions, /v1/responses, /v1/messages).
  - If the request body contains "mock_format": "<fmt>", that wins.
  - If the request body contains "mock_status": <code>, that HTTP status is
    returned instead (error-path testing; bypasses strict validation).
  - "mock_simple_stream": true returns the plain content-only stream (no
    reasoning_content / cost frames).
  - Streaming: request body "stream": true returns an SSE stream.

Usage:
    python3 scripts/mock_upstream.py --port 9100 [--log /tmp/mock.log]
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_FILE = "/tmp/mock_upstream.log"

# Exact error body returned by the real opencode.ai "Console Go" backend for
# request-validation failures (observed 2026-08).
REAL_ERROR_BODY = {
    "error": {
        "message": "Error from provider (Console Go): Upstream request failed",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request_error",
    }
}


def log(req):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")


def validate_chat_completions(req):
    """Mirror the strict OpenAI chat-completions validation of opencode.ai.

    Returns (ok, reason); on !ok the server answers 400 with REAL_ERROR_BODY.
    """
    for m in req.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "developer":
            return False, "role 'developer' is not valid for chat completions"
    for t in req.get("tools", []):
        if not isinstance(t, dict):
            continue
        ttype = t.get("type", "function")
        if ttype != "function":
            return False, "tool type %r is not representable in chat completions" % ttype
        fn = t.get("function", {})
        params = fn.get("parameters") if isinstance(fn, dict) else None
        if params is not None and (
            not isinstance(params, dict) or params.get("type") != "object"
        ):
            return False, "tool parameters must be a JSON Schema with type:\"object\""
    return True, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len("{}")))
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        raw = self._read_body()
        try:
            req = json.loads(raw) if raw else {}
        except Exception:
            req = {}
        fmt = req.get("mock_format")
        if not fmt:
            p = self.path
            if p.endswith("/messages"):
                fmt = "anthropic"
            elif p.endswith("/responses"):
                fmt = "responses"
            else:
                fmt = "openai"
        status = req.get("mock_status", 200)
        if req.get("model") == "trigger-error":
            status = 400  # model-based trigger survives format conversion

        # Hold the connection open before responding — lets tests exercise the
        # proxy's upstream read timeout / client-disconnect abort.
        delay = req.get("mock_delay", 0)
        if delay:
            time.sleep(delay)

        log({
            "path": self.path,
            "method": self.command,
            "auth": self.headers.get("Authorization"),
            "x_api_key": self.headers.get("x-api-key"),
            "anthropic_version": self.headers.get("anthropic-version"),
            "content_type": self.headers.get("Content-Type"),
            "body": req,
        })

        stream = bool(req.get("stream")) and status == 200
        model = req.get("model", "mock-model")

        # Explicit status override (error-path testing) wins; otherwise the
        # strict chat-completions validation behaves like the real upstream.
        if status != 200:
            body = {
                "error": {"message": "mock error", "type": "mock_error", "code": status},
            }
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if fmt == "openai":
            ok, reason = validate_chat_completions(req)
            if not ok:
                payload = json.dumps(REAL_ERROR_BODY).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = self._stream_chunks(fmt, model, req)
            for c in chunks:
                if isinstance(c, str):
                    c = c.encode()
                # Proper HTTP/1.1 chunk framing.
                self.wfile.write(b"%x\r\n" % len(c))
                self.wfile.write(c)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        body = self._nonstream_body(fmt, model, req)
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _openai_chunk(self, cid, model, delta, finish_reason=None):
        return {
            "id": cid, "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{
                "index": 0, "finish_reason": finish_reason, "logprobs": None,
                "delta": delta,
            }],
            "usage": None,
        }

    def _stream_chunks(self, fmt, model, req=None):
        req = req or {}
        if fmt == "anthropic":
            usage = '{"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}'
            return [
                f'event: message_start\ndata: {{"type":"message_start","message":{{"id":"m1","type":"message","role":"assistant","model":"{model}","content":[],"usage":{usage}}}}}\n\n',
                f'event: content_block_start\ndata: {{"type":"content_block_start","index":0,"content_block":{{"type":"text","text":""}}}}\n\n',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n\n',
                'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n\n',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
                'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":7,"input_tokens":11}}\n\n',
                'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]
        if fmt == "responses":
            return [
                'data: {"type":"response.created","response":{"id":"resp1","object":"response","status":"in_progress","model":"' + model + '"}}\n\n',
                'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"i1","type":"message","role":"assistant","content":[]}}\n\n',
                'data: {"type":"response.content_part.added","item_id":"i1","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}\n\n',
                'data: {"type":"response.output_text.delta","item_id":"i1","output_index":0,"content_index":0,"delta":"Hel"}\n\n',
                'data: {"type":"response.output_text.delta","item_id":"i1","output_index":0,"content_index":0,"delta":"lo"}\n\n',
                'data: {"type":"response.output_text.done","item_id":"i1","output_index":0,"content_index":0,"text":"Hello"}\n\n',
                'data: {"type":"response.content_part.done","item_id":"i1","output_index":0,"content_index":0,"part":{"type":"output_text","text":"Hello","annotations":[]}}\n\n',
                'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"i1","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Hello","annotations":[]}]}}\n\n',
                'data: {"type":"response.completed","response":{"id":"resp1","object":"response","status":"completed","model":"' + model + '","output":[{"id":"i1","type":"message","status":"completed","role":"assistant","content":[{"type":"output_text","text":"Hello","annotations":[]}]}],"usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18,"input_tokens_details":{"cached_tokens":3}}}}\n\n',
            ]

        # ── OpenAI chat completions ──────────────────────────────────────
        cid = "chatcmpl-mock1"
        # Tool-call stream — used to exercise the OpenAI→Anthropic
        # tool-conversion path end-to-end.  Triggered by the request model
        # name "mock-tool" (survives proxy format conversion) or the
        # "mock_tool" body flag.  Prefixed with reasoning deltas, matching the
        # real DeepSeek-family backend's thinking-before-tool pattern.
        if req.get("mock_tool") or model == "mock-tool":
            chunks = [
                json.dumps(self._openai_chunk(
                    cid, model,
                    {"role": "assistant", "content": None, "reasoning_content": ""})),
                json.dumps(self._openai_chunk(
                    cid, model,
                    {"content": None, "reasoning_content": "Need to fetch weather."})),
                json.dumps(self._openai_chunk(
                    cid, model,
                    {"content": None, "reasoning_content": "Calling get_weather."})),
                json.dumps(self._openai_chunk(
                    cid, model,
                    {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                     "function": {"name": "get_weather",
                                                  "arguments": ""}}]})),
                json.dumps(self._openai_chunk(
                    cid, model,
                    {"tool_calls": [{"index": 0,
                                     "function": {"arguments": "{\"city\":\"SF\"}"}}]})),
                json.dumps(self._openai_chunk(cid, model, {}, "tool_calls")),
                json.dumps({"id": cid, "object": "chat.completion.chunk",
                            "created": 1, "model": model, "choices": [],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                      "total_tokens": 18}}),
            ]
            return ["data: " + c + "\n\n" for c in chunks] + ["data: [DONE]\n\n"]

        if req.get("mock_simple_stream"):
            return [
                'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n',
                'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n',
                'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
                'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n',
                'data: [DONE]\n\n',
            ]

        # Realistic DeepSeek-family stream (as observed from opencode.ai):
        # role-start → reasoning_content deltas → content deltas →
        # finish:"stop" → x-opencode cost frame → [DONE].
        chunks = [
            self._openai_chunk(cid, model,
                               {"role": "assistant", "content": None,
                                "reasoning_content": ""}),
        ]
        for r in ("We need to answer simply.", "User just says hi."):
            chunks.append(self._openai_chunk(
                cid, model, {"content": None, "reasoning_content": r}))
        for t in ("Hel", "lo"):
            chunks.append(self._openai_chunk(
                cid, model, {"content": t, "reasoning_content": None}))
        chunks.append(self._openai_chunk(cid, model, {"content": ""}, "stop"))
        frames = ["data: " + json.dumps(c) + "\n\n" for c in chunks]
        # opencode.ai-specific inference-cost usage frame (non-standard).
        frames.append('data: {"choices":[],"x-opencode-type":"inference-cost",'
                      '"cost":"0.00000123","normalizedUsage":{"inputTokens":11,'
                      '"outputTokens":7,"reasoningTokens":5,"cacheReadTokens":0,'
                      '"cacheWrite5mTokens":0,"cacheWrite1hTokens":0}}\n\n')
        frames.append('data: [DONE]\n\n')
        return frames

    def _nonstream_body(self, fmt, model, req):
        usage_extra = req.get("mock_usage_extra", {})
        if fmt == "anthropic":
            return {
                "id": "msg_1", "type": "message", "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {
                    "input_tokens": 11, "output_tokens": 7,
                    "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
                    **usage_extra,
                },
            }
        if fmt == "responses":
            return {
                "id": "resp_1", "object": "response", "created_at": 1,
                "status": "completed", "model": model,
                "output": [
                    {"id": "i1", "type": "message", "status": "completed",
                     "role": "assistant",
                     "content": [{"type": "output_text", "text": "Hello", "annotations": []}]},
                ],
                "usage": {
                    "input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                    "input_tokens_details": {"cached_tokens": 3}, **usage_extra,
                },
            }
        # OpenAI chat completions, non-streaming.
        return {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                      **usage_extra},
        }

    def log_message(self, fmt, *args):
        pass  # silence default stderr logging


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--log", default="/tmp/mock_upstream.log")
    args = ap.parse_args()
    global LOG_FILE
    LOG_FILE = args.log
    open(LOG_FILE, "w", encoding="utf-8").close()  # truncate at start
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Mock upstream listening on http://127.0.0.1:{args.port} (log: {LOG_FILE})",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
