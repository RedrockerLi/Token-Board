#!/usr/bin/env python3
"""Mock upstream AI API server for testing the proxy.

Speaks all three wire formats (OpenAI chat completions, OpenAI Responses,
Anthropic Messages).  Logs every request (path, headers, body) to
`request_log.txt` next to this script so tests can assert on what the proxy
actually sent upstream.

Usage:
    python3 scripts/mock_upstream.py --port 9100 [--log /tmp/mock.log]

Response selection:
  - By default the format is inferred from the URL path
    (/v1/chat/completions, /v1/responses, /v1/messages).
  - If the request body contains "mock_format": "<fmt>", that wins.
  - If the request body contains "mock_status": <code>, that HTTP status is
    returned instead (error-path testing).
  - Streaming: request body "stream": true returns an SSE stream.
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_FILE = "/tmp/mock_upstream.log"


def log(req):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")


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

    def _stream_chunks(self, fmt, model, req=None):
        req = req or {}
        # Tool-call stream (only for the OpenAI chat format) — used to exercise
        # the OpenAI→Anthropic tool-conversion path end-to-end.  Triggered by the
        # request model name "mock-tool" (survives proxy format conversion) or the
        # "mock_tool" body flag.
        if (req.get("mock_tool") or model == "mock-tool") and fmt == "openai":
            return [
                'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n',
                'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}\n\n',
                'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":\\"SF\\"}"}}]},"finish_reason":null}]}\n\n',
                'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
                'data: {"id":"c2","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n',
                'data: [DONE]\n\n',
            ]
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
        # openai chat
        return [
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n',
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n',
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"' + model + '","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}\n\n',
            'data: [DONE]\n\n',
        ]

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
