#!/usr/bin/env python3
"""V1 normalized route-set/credential end-to-end smoke test."""

import json
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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(port):
    for _ in range(150):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.2) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(.02)
    raise AssertionError("proxy did not become healthy")


def main():
    binary = Path(sys.argv[1]).resolve()
    schema = Path(sys.argv[2]).resolve()
    root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(root))
    from app.db.migrations import migrate
    from scripts.mock_upstream import Handler

    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "token-board.db"
        migrate(str(db), str(schema), "token-board")
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO accounts(id,uuid,name) VALUES(1,'account-1','v1')")
        conn.execute(
            "INSERT INTO upstreams(id,account_id,name,base_url,api_format,auth_scheme,max_concurrency) "
            "VALUES(1,1,'mock',?,'openai','bearer',128)",
            (f"http://127.0.0.1:{upstream_port}",))
        conn.execute("INSERT INTO route_sets(id,uuid,account_id,name) VALUES(1,'route-1',1,'v1')")
        conn.execute(
            "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
            "VALUES(1,'*',0,1)")
        conn.execute(
            "INSERT INTO client_keys(id,uuid,key_value,label,route_set_id) "
            "VALUES(1,'client-1','tb-v1','v1',1)")
        conn.execute(
            "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked) "
            "VALUES('credential-1',1,1,0,'sk-…mock')")
        conn.execute(
            "INSERT INTO upstream_secrets(credential_uuid,secret_value) "
            "VALUES('credential-1','sk-mock')")
        conn.execute(
            "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,valid_from) "
            "VALUES('contract-1',1,'metered','account','2020-01-01')")
        conn.commit()
        initial_generation = conn.execute(
            "SELECT generation FROM config_state WHERE id=1").fetchone()[0]
        assert initial_generation > 1
        conn.close()

        proxy_port = free_port()
        proxy = subprocess.Popen(
            [str(binary), "--db", str(db), "--schema-dir", str(schema),
             "--host", "127.0.0.1", "--port", str(proxy_port),
             "--log-level", "error"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_health(proxy_port)
            payload = json.dumps({
                "model": "v1-model", "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                data=payload,
                headers={"Authorization": "Bearer tb-v1",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode()
                assert response.status == 200
            assert "data:" in body
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                conn = sqlite3.connect(db)
                row = conn.execute(
                    "SELECT account_id,route_set_id,client_key_id,credential_uuid,"
                    "model,status_code FROM request_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                conn.close()
                if row:
                    break
                time.sleep(.03)
            assert row == (1, 1, 1, "credential-1", "v1-model", 200), row
        finally:
            proxy.terminate()
            proxy.wait(timeout=5)
            if proxy.returncode not in (0, -15):
                raise AssertionError(proxy.stderr.read())
    upstream.shutdown(); upstream.server_close(); thread.join(timeout=2)
    print("V1 normalized routing passed")


if __name__ == "__main__":
    main()
