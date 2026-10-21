#!/usr/bin/env python3
"""OpenCode Go session header + usage-aware key scheduling (end-to-end).

Drives the real proxy against mock_upstream with "opencode-go" upstreams
(upstreams.subtype='opencode-go', schema V2.7) and asserts:

  S1  a request without any downstream session identity still reaches the
      upstream with a `tb-…` x-opencode-session and User-Agent token-board/1.0
      (mock_require_session turns a missing header into a hard 400).  The
      conversation fingerprint repeats identically for the same prompt, and a
      request with no identifiable conversation gets a fresh random id.
  S2  a downstream x-claude-code-session-id is forwarded verbatim, while an
      explicit downstream x-opencode-session outranks it.
  S3  seeded usage_json scheduling: the soonest-resetting usable credential is
      tried FIRST and a credential with an exhausted window is tried LAST.
  S4  429 on one key with a healthy sibling → downstream 200 with both keys
      recorded, on the non-streaming and the streaming path.
  S5  every key 429 → the downstream client receives the error.
  S6  the cooldown probe (GET /models) carries the session header too: a cooled
      key is cleared early while the mock demands the header on every GET.
  S7  the /v1/models passthrough reaches the same session-policed upstream.
  S8  embeddings requests carry the session header as well.
  S9  a lapsed subscription (EntitlementError verdict in usage_json) is
      demoted behind healthy and exhausted keys and rejoins normal
      scheduling as soon as an observation says the subscription is back.

Usage:
  python3 opencode_session_test.py <token_proxy> <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
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


def iso_after(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


# Every key of one subscription reports the same absolute week/month period
# ends (the upstream's calendar boundaries do not depend on when a key was
# queried).  Pin them once so sibling ordering is decided by the rolling
# window — never by per-write timestamp offsets.
_BOUNDARY_EPOCH = time.time()


def _iso_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def usage_snapshot(percent: int, resets_in: float) -> str:
    """The frozen usage_json shape the Python poller writes."""
    return json.dumps({
        "checked_at": iso_after(0),
        "error": None,
        "subscription": "active",
        "windows": {
            "5h": {"percent": percent, "resets_at": iso_after(resets_in),
                   "state": "started"},
            "week": {"percent": 3,
                     "resets_at": _iso_epoch(_BOUNDARY_EPOCH + 3 * 86400),
                     "state": "started"},
            "month": {"percent": 2,
                      "resets_at": _iso_epoch(_BOUNDARY_EPOCH + 20 * 86400),
                      "state": "started"},
        },
        "monthly_period": {"start": iso_after(-86400),
                           "end": iso_after(29 * 86400)},
    })


def lapsed_snapshot() -> str:
    """The usage_json shape the poller writes after an EntitlementError 403."""
    return json.dumps({
        "error": "HTTP 403",
        "error_at": iso_after(0),
        "subscription": "inactive",
        "windows": {},
        "monthly_period": None,
    })


def send(proxy_port, local_key, body, headers=None, timeout=30,
         path="/v1/chat/completions"):
    request_headers = {"Authorization": f"Bearer {local_key}",
                       "Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}{path}",
        data=json.dumps(body).encode(),
        headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def read_log(log_path, marker):
    """Mock request records whose body carries the given marker."""
    if not Path(log_path).exists():
        return []
    records = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if (record.get("body") or {}).get("mock_marker") == marker:
            records.append(record)
    return records


def ctrl(upstream_port, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{upstream_port}{path}",
                                 data=data,
                                 headers={"Content-Type": "application/json"})
    req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def set_usage(db_path, runtime_id, snapshot):
    """Replace one credential's usage_json (what the poller would write)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE upstream_credentials SET usage_json=? "
                     "WHERE runtime_id=?", (snapshot, runtime_id))
        conn.commit()
    finally:
        conn.close()


def send_until_served(db_path, proxy_port, local_key, body, model, key_id,
                      min_id, deadline=8.0):
    """Send until a request is served by *key_id*.

    The usage watch lifts a cooldown on its own cadence, so recovery is
    polled (one request per idle cycle) instead of timed.
    """
    end = time.monotonic() + deadline
    rid = min_id
    statuses, keys = [], []
    while time.monotonic() < end:
        send(proxy_port, local_key, body)
        rid, _, statuses, keys = latest_attempts(db_path, model, rid)
        if keys and keys[0] == key_id:
            return rid, statuses, keys
        time.sleep(0.4)
    raise AssertionError(
        f"{model}: key {key_id} was never served: {statuses} {keys}")


def latest_attempts(db_path, model, min_id=0):
    """(row_id, attempt_count, [statuses], [upstream_key_ids]) for the newest row."""
    conn = sqlite3.connect(db_path)
    try:
        for _ in range(120):
            row = conn.execute(
                "SELECT id, attempt_count FROM request_log "
                "WHERE model=? AND id>? ORDER BY id DESC LIMIT 1",
                (model, min_id),
            ).fetchone()
            if row:
                attempts = conn.execute(
                    "SELECT status_code, upstream_key_id FROM request_attempts "
                    "WHERE request_log_id=? ORDER BY attempt_index", (row[0],),
                ).fetchall()
                return (row[0], row[1],
                        [a[0] for a in attempts], [a[1] for a in attempts])
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
    import scripts.mock_upstream as mock_upstream
    from scripts.mock_upstream import Handler, ThreadingHTTPServer
    from v1_fixture import (add_plain_route, add_upstream,
                            build_aggregate_scenario, ensure_v2_database)

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "mock_upstream.log"
        mock_upstream.LOG_FILE = str(log_path)
        upstream_port = free_port()
        upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()

        db_path = Path(tmp) / "token-board.db"
        ensure_v2_database(db_path, schema_dir)
        conn = sqlite3.connect(db_path)
        base_url = f"http://127.0.0.1:{upstream_port}"
        try:
            # Declaration order is [exhausted, lead, late] so a correct run has
            # to reorder all three by usage.
            _, ordering_keys, _ = build_aggregate_scenario(
                conn, "tb-oc1", "oc1-model",
                ["sk-oc1-exhausted", "sk-oc1-lead", "sk-oc1-late"], None,
                subtype="opencode-go")
            build_aggregate_scenario(
                conn, "tb-oc2", "oc2-model", ["sk-oc2-p1", "sk-oc2-p2"],
                None, subtype="opencode-go")
            build_aggregate_scenario(
                conn, "tb-oc3", "oc3-model", ["sk-oc3-p1"], None,
                subtype="opencode-go")
            # S9: one lapsed key (no subscription) beside one healthy key.
            _, lapse_keys, _ = build_aggregate_scenario(
                conn, "tb-oc6", "oc6-model", ["sk-oc6-p1", "sk-oc6-p2"], None,
                subtype="opencode-go")
            # Cooldown-recovery scenario (cooled key) and a plain account
            # whose catalog/embeddings calls hit the upstream directly.
            _, probe_keys, _ = build_aggregate_scenario(
                conn, "tb-oc4", "oc4-model", ["sk-oc4-p1", "sk-oc4-p2"], None,
                subtype="opencode-go")
            plain_id, plain_upstream, _ = add_upstream(
                conn, "tb-oc5", "", ["sk-oc5-p1"], subtype="opencode-go")
            add_plain_route(conn, "tb-oc5", "tb-oc5-route", plain_upstream,
                            plain_id, "*")
            conn.execute("UPDATE upstreams SET base_url=?", (base_url,))
            for runtime_id, (percent, resets_in) in zip(
                    ordering_keys, [(100, 300), (20, 600), (20, 7200)]):
                conn.execute(
                    "UPDATE upstream_credentials SET usage_json=? "
                    "WHERE runtime_id=?",
                    (usage_snapshot(percent, resets_in), runtime_id))
            # S6: the usage snapshot drives cooldown recovery.  p1 leads the
            # rotation (its quota resets soonest) and p2 has room too, so the
            # cooling/recooling order below is deterministic.
            for runtime_id, (percent, resets_in) in zip(
                    probe_keys, [(30, 600), (20, 7200)]):
                conn.execute(
                    "UPDATE upstream_credentials SET usage_json=? "
                    "WHERE runtime_id=?",
                    (usage_snapshot(percent, resets_in), runtime_id))
            # S9: p1's subscription is gone (the poller's EntitlementError
            # verdict, no windows at all); p2 is healthy with a modest
            # pressure signal so any normal ordering rule would prefer p2
            # only via the demotion.
            lapsed_key, lapse_sibling = lapse_keys
            conn.execute(
                "UPDATE upstream_credentials SET usage_json=? WHERE runtime_id=?",
                (lapsed_snapshot(), lapsed_key))
            conn.execute(
                "UPDATE upstream_credentials SET usage_json=? WHERE runtime_id=?",
                (usage_snapshot(20, 7200), lapse_sibling))
            # S7: tb-oc5's plain account carries a stored catalog.
            conn.executemany(
                "INSERT INTO upstream_model_catalog(upstream_id, model_id) "
                "VALUES(?,?)",
                [(plain_upstream, "oc5-catalog-a"),
                 (plain_upstream, "oc5-catalog-b")])
            conn.commit()
        finally:
            conn.close()

        proxy_port = free_port()
        # Short cadences so S6 reaches the recovery cycles within the run.
        env = {**os.environ, "TB_COOLDOWN_PROBE_SECS": "2",
               "TB_USAGE_WATCH_SECS": "2"}
        command = [str(proxy_binary), "--db", str(db_path), "--host",
                   "127.0.0.1", "--port", str(proxy_port)]
        if os.environ.get("TB_TEST_DEBUG"):
            command.extend(["--log-level", "debug"])
        proxy = subprocess.Popen(
            command,
            stdout=None if os.environ.get("TB_TEST_DEBUG") else subprocess.DEVNULL,
            stderr=None if os.environ.get("TB_TEST_DEBUG") else subprocess.PIPE,
            text=True, env=env)
        try:
            wait_for_health(proxy_port)

            # ── S1: session id injected upstream, fingerprint stable ─────
            body = {"stream": False, "model": "oc3-model",
                    "messages": [{"role": "user", "content": "hello session"}],
                    "mock_require_session": True, "mock_marker": "s1a"}
            status, _ = send(proxy_port, "tb-oc3", body)
            assert status == 200, \
                f"S1: a session-less client must still be routable (got {status})"
            send(proxy_port, "tb-oc3", body)
            records = read_log(log_path, "s1a")
            assert len(records) == 2, f"S1: expected 2 mock hits, got {len(records)}"
            sessions = [r.get("x_opencode_session") or "" for r in records]
            assert all(s.startswith("tb-") and len(s) == 35 for s in sessions), \
                f"S1: upstream session id shape {sessions}"
            assert sessions[0] == sessions[1], \
                f"S1: the same conversation must fingerprint identically: {sessions}"
            agents = {r.get("user_agent") for r in records}
            assert agents == {"token-board/1.0"}, f"S1: upstream User-Agent {agents}"
            print(f"S1 OK: session-less request injected {sessions[0]} "
                  f"with User-Agent token-board/1.0 (stable fingerprint)")

            # A body with no system prompt and no user turn carries no
            # conversation identity: each request gets its own session.
            random_body = {"stream": False, "model": "oc3-model",
                           "messages": [{"role": "assistant", "content": "x"}],
                           "mock_require_session": True, "mock_marker": "s1b"}
            send(proxy_port, "tb-oc3", random_body)
            send(proxy_port, "tb-oc3", random_body)
            randoms = [r.get("x_opencode_session") or ""
                       for r in read_log(log_path, "s1b")]
            assert len(randoms) == 2 and all(
                s.startswith("tb-") and len(s) == 35 for s in randoms), randoms
            assert randoms[0] != randoms[1], \
                f"S1: an unidentified conversation must not share a session {randoms}"
            print("S1 OK: no identifiable conversation -> per-request random session")

            # ── S2: downstream session headers are forwarded verbatim ───
            body = {"stream": False, "model": "oc3-model",
                    "messages": [{"role": "user", "content": "session s2"}],
                    "mock_marker": "s2a"}
            send(proxy_port, "tb-oc3", body,
                 headers={"x-claude-code-session-id": "cc-session-77"})
            forwarded = read_log(log_path, "s2a")[0].get("x_opencode_session")
            assert forwarded == "cc-session-77", forwarded

            send(proxy_port, "tb-oc3", {**body, "mock_marker": "s2b"},
                 headers={"x-claude-code-session-id": "cc-session-77",
                          "x-opencode-session": "explicit-abc"})
            forwarded = read_log(log_path, "s2b")[0].get("x_opencode_session")
            assert forwarded == "explicit-abc", forwarded
            print("S2 OK: x-claude-code-session-id forwarded verbatim; "
                  "x-opencode-session outranks it")

            # ── S3/S4: usage ordering + sibling fallback (non-streaming) ─
            exhausted, lead, late = ordering_keys
            flags = {"mock_status_by_key": {"sk-oc1-lead": 429,
                                            "sk-oc1-late": 429,
                                            "sk-oc1-exhausted": 200}}
            common = {"model": "oc1-model",
                      "messages": [{"role": "user", "content": "order"}]}
            status, _ = send(proxy_port, "tb-oc1",
                             {**common, "stream": False, **flags})
            assert status == 200, f"S3: sibling fallback must succeed ({status})"
            rid, _, statuses, keys = latest_attempts(db_path, "oc1-model")
            assert statuses == [429, 429, 200], f"S3 attempts {statuses}"
            assert keys == [lead, late, exhausted], \
                f"S3 must try lead first and the exhausted key last: {keys} " \
                f"(lead={lead} late={late} exhausted={exhausted})"
            print(f"S3 OK: usage order lead->late->exhausted "
                  f"(attempts={statuses}, keys={keys})")

            # S3's plain 429s put lead and late into their short rate-limit
            # avoid, so the immediate streaming request goes straight to the
            # one remaining key — a spent window is the last resort only when
            # nothing else is available.
            status, text = send(proxy_port, "tb-oc1",
                                {**common, "stream": True, **flags})
            rid, _, statuses, keys = latest_attempts(db_path, "oc1-model", rid)
            assert status == 200 and "[DONE]" in text, f"S4 streaming status {status}"
            assert statuses == [200] and keys == [exhausted], \
                f"S4 must go straight to the remaining key: {statuses} {keys}"
            print("S4 OK: sibling 429s hold their avoid; streaming reaches "
                  f"the remaining key directly (attempts={statuses})")

            # ── S5: every key 429 → the client receives the error ───────
            status, text = send(proxy_port, "tb-oc2", {
                "stream": False, "model": "oc2-model",
                "messages": [{"role": "user", "content": "all fail"}],
                "mock_status_by_key": {"sk-oc2-p1": 429, "sk-oc2-p2": 429}})
            assert status == 429, f"S5 downstream status {status}"
            assert "error" in text, text[:200]
            _, _, statuses, _ = latest_attempts(db_path, "oc2-model")
            assert statuses == [429, 429], f"S5 attempts {statuses}"
            print(f"S5 OK: all keys 429 -> downstream 429 (attempts={statuses})")

            # ── S1 (streaming): the session reaches the SSE forward too ──
            stream_body = {"stream": True, "model": "oc3-model",
                           "messages": [{"role": "user", "content": "stream s1"}],
                           "mock_require_session": True, "mock_marker": "s1c"}
            status, text = send(proxy_port, "tb-oc3", stream_body)
            assert status == 200 and "[DONE]" in text, f"S1 streaming {status}"
            stream_record = read_log(log_path, "s1c")[0]
            assert (stream_record.get("x_opencode_session") or "").startswith("tb-"), \
                stream_record.get("x_opencode_session")
            print("S1 OK: the streaming forward carries the session header too")

            # ── S6: an opencode-go cooldown recovers from the usage snapshot ──
            # The gateway's catalog answers 200 for any or no key, so an HTTP
            # probe cannot witness recovery; the usage snapshot the poller
            # refreshes is the signal.  The recovery is observed on the
            # usage-watch cadence and must never send a request upstream.
            cooled, sibling = probe_keys
            send(proxy_port, "tb-oc4", {
                "stream": False, "model": "oc4-model",
                "messages": [{"role": "user", "content": "cool p1"}],
                "mock_status_by_key": {"sk-oc4-p1": 429},
                "mock_error_type_by_key": {"sk-oc4-p1": "GoUsageLimitError"}})
            rid, _, statuses, keys = latest_attempts(db_path, "oc4-model")
            assert statuses == [429, 200] and keys[1] == sibling, (statuses, keys)
            # Simulate the poller's next observation after the window reset:
            # the rolling window began a new cycle, so its reset stamp has
            # moved — the evidence the usage watch requires before lifting
            # the cooldown (a merely similar percentage is not enough).
            set_usage(db_path, cooled, usage_snapshot(5, 300))
            rid, statuses, keys = send_until_served(
                db_path, proxy_port, "tb-oc4",
                {"stream": False, "model": "oc4-model",
                 "messages": [{"role": "user", "content": "cool p1 again"}]},
                "oc4-model", cooled, rid)
            assert statuses == [200], \
                f"S6: the recovered key must serve again (attempts={statuses}, {keys})"
            probes = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"]
            assert probes.get("sk-oc4-p1", 0) == 0, \
                f"S6: a usage-watched key must never be probed (probes={probes})"
            print("S6 OK: the usage snapshot cleared the cooldown with no probe")

            # ── S6b: exhausted usage holds the cooldown; recovery releases it ──
            set_usage(db_path, cooled, usage_snapshot(100, 300))
            time.sleep(0.5)  # let the exhausted snapshot hot-reload first
            send(proxy_port, "tb-oc4", {
                "stream": False, "model": "oc4-model",
                "messages": [{"role": "user", "content": "recool exhausted"}],
                "mock_status_by_key": {"sk-oc4-p2": 429, "sk-oc4-p1": 429},
                "mock_error_type_by_key": {"sk-oc4-p1": "GoUsageLimitError"},
                # The sibling's plain 429 must not hold it for a minute: the
                # re-cool request is followed by requests seconds later.
                "mock_retry_after": 1})
            # Usage-exhausted p1 is scheduled last, so p2 answers 429 first
            # and p1 consumes the retry, re-entering the cooldown.
            rid, _, statuses, keys = latest_attempts(db_path, "oc4-model", rid)
            assert statuses == [429, 429] and keys[1] == cooled, (statuses, keys)
            time.sleep(3.0)  # usage-watch cycles see 100% -> stays cooling
            send(proxy_port, "tb-oc4", {
                "stream": False, "model": "oc4-model",
                "messages": [{"role": "user", "content": "still cooling"}]})
            rid, _, statuses, keys = latest_attempts(db_path, "oc4-model", rid)
            assert keys == [sibling], \
                f"S6b: an exhausted key stays cooling (attempts={statuses}, {keys})"
            set_usage(db_path, cooled, usage_snapshot(18, 600))
            _, statuses, keys = send_until_served(
                db_path, proxy_port, "tb-oc4",
                {"stream": False, "model": "oc4-model",
                 "messages": [{"role": "user", "content": "recovered again"}]},
                "oc4-model", cooled, rid)
            assert statuses == [200], \
                f"S6b: recovery must clear the cooldown again (attempts={statuses}, {keys})"
            print("S6b OK: exhausted usage held the cooldown; recovery released it")

            # ── S7: /v1/models answers from the stored catalog, no upstream ──
            req = urllib.request.Request(
                f"http://127.0.0.1:{proxy_port}/v1/models",
                headers={"Authorization": "Bearer tb-oc5"})
            with urllib.request.urlopen(req, timeout=30) as r:
                models_status, models_body = r.status, r.read().decode()
            assert models_status == 200, \
                f"S7: models catalog {models_status} {models_body[:120]}"
            data = json.loads(models_body)
            assert [m["id"] for m in data["data"]] == \
                ["oc5-catalog-a", "oc5-catalog-b"], models_body
            probes = ctrl(upstream_port, "GET", "/__ctrl/status")["probes"]
            assert probes.get("sk-oc5-p1", 0) == 0, \
                f"S7: the catalog is a local read (probes={probes})"
            print("S7 OK: /v1/models served the stored catalog")

            # ── S8: embeddings carries the session header ────────────────
            status, _ = send(proxy_port, "tb-oc5", {
                "model": "text-embedding-mock", "input": "hello",
                "mock_require_session": True, "mock_marker": "s8"},
                path="/v1/embeddings")
            record = read_log(log_path, "s8")[0]
            assert (record.get("x_opencode_session") or "").startswith("tb-"), \
                (status, record.get("x_opencode_session"))
            print("S8 OK: embeddings carries the session header")

            # ── S9: a lapsed subscription is demoted behind everything ────
            # p1 holds an EntitlementError verdict (no windows at all): every
            # request must be served by the healthy sibling, request after
            # request — the rotation must never surface the lapsed key while
            # anything else exists.
            for hop in range(3):
                status, _ = send(proxy_port, "tb-oc6", {
                    "stream": False, "model": "oc6-model",
                    "messages": [{"role": "user", "content": f"lapsed {hop}"}]})
                rid, _, statuses, keys = latest_attempts(db_path, "oc6-model")
                assert status == 200 and statuses == [200] \
                    and keys == [lapse_sibling], \
                    f"S9: lapsed key must stay demoted (hop={hop}, {statuses}, {keys})"
            # The subscription is observed again: the key rejoins normal
            # scheduling immediately (its rolling pressure now leads).
            set_usage(db_path, lapsed_key, usage_snapshot(90, 600))
            time.sleep(0.5)  # let the new snapshot hot-reload
            status, _ = send(proxy_port, "tb-oc6", {
                "stream": False, "model": "oc6-model",
                "messages": [{"role": "user", "content": "lapsed recovered"}]})
            rid, _, statuses, keys = latest_attempts(db_path, "oc6-model", rid)
            assert status == 200 and statuses == [200] and keys == [lapsed_key], \
                f"S9: recovery must restore normal scheduling ({statuses}, {keys})"
            print("S9 OK: the lapsed key stayed demoted and rejoined on recovery")

            print("opencode session tests passed")
        finally:
            diagnostics = stop_process(proxy, check_returncode=False)
            if proxy.returncode not in (0, -15):
                print(diagnostics, file=sys.stderr)

        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    main()
