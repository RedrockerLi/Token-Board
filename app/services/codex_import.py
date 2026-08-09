"""Codex session usage importer — background, best-effort, idempotent.

Parses Codex CLI session transcripts from ``~/.codex/sessions`` (recursively;
date-partitioned ``YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl``, plain JSONL;
``.jsonl.gz`` accepted for robustness) and inserts one ``request_log`` row per
``token_count`` event (per-turn granularity, using ``last_token_usage`` deltas),
attributed to the first ``agent_kind='codex'`` account.

Idempotency: each row's ``event_id`` is deterministic
(``codex:<session_id>:<line_no>``) and ``insert_agent_usage`` uses
``INSERT OR IGNORE``, so re-scanning a file — including after a crash — never
double-counts.  ``codex_import_state`` records ``(size, mtime)`` so unchanged
files are skipped entirely on later passes.

Never raises into the request path: every error is logged and swallowed.
"""

import gzip
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

CODEX_DIR = Path.home() / ".codex" / "sessions"
SCAN_INTERVAL_SECONDS = 60

# Models that must never be imported (noise from Codex sub-agents / auto-review
# turns).  Rows already imported for these models are deleted on every pass.
EXCLUDED_MODELS = {"codex-auto-review"}


def _iso_z_to_sqlite(ts: str) -> str | None:
    """'2026-08-04T16:37:44.757Z' → '2026-08-04 16:37:44' (SQLite UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _session_id_from_path(path: Path) -> str:
    """Extract the session UUID from the filename, with fallbacks."""
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # rollout-<ts>-<uuid> → keep the trailing 36-char UUID; else the stem.
    for part in reversed(name.split("-")):
        if len(part) == 36 and part.count("-") == 4:
            return part
    return path.stem


def _open_maybe_gz(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _iter_session_files():
    if not CODEX_DIR.is_dir():
        return
    seen = set()
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for p in CODEX_DIR.rglob(pattern):
            if not p.is_file():
                continue
            key = p.resolve()
            if key not in seen:
                seen.add(key)
                yield p


def _parse_session(path: Path):
    """Parse one session file → (total_line_count, [row dicts]).

    Each ``token_count`` event_msg yields one row using its ``last_token_usage``
    (per-turn delta).  Model is the last ``turn_context.payload.model`` seen.
    """
    rows = []
    session_id = None
    model = "codex"  # fallback when no turn_context carries a model
    line_no = 0
    for raw in _open_maybe_gz(path):
        line_no += 1
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        payload = obj.get("payload") or {}
        if etype == "session_meta":
            if payload.get("session_id"):
                session_id = payload["session_id"]
        elif etype == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                model = m.strip()
        elif etype == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            if not isinstance(last, dict):
                continue
            if model in EXCLUDED_MODELS:
                continue  # noise (auto-review sub-agent) — never imported
            ts = _iso_z_to_sqlite(obj.get("timestamp"))
            if ts is None:
                continue
            event_id = "codex:{}:{}".format(
                session_id or _session_id_from_path(path), line_no)
            rows.append({
                "model": model,
                "prompt_tokens": int(last.get("input_tokens") or 0),
                "completion_tokens": int(last.get("output_tokens") or 0),
                "cache_read_tokens": int(last.get("cached_input_tokens") or 0),
                "total_tokens": int(last.get("total_tokens") or 0),
                "requested_at": ts,
                "event_id": event_id,
            })
    return line_no, rows


def run_import(pdb, stop_event: threading.Event | None = None,
               once: bool = False) -> int:
    """Scan codex sessions and import new usage into the proxy database.

    Returns the number of rows inserted this pass.  Loops every
    SCAN_INTERVAL_SECONDS until ``stop_event`` is set (or runs once when
    ``once=True``, used by tests / manual triggers).
    """
    total = 0
    while True:
        try:
            total += _import_pass(pdb)
        except Exception:
            log.exception("codex import pass failed (will retry)")
        if once:
            return total
        if stop_event is not None and stop_event.wait(SCAN_INTERVAL_SECONDS):
            return total


def _import_pass(pdb) -> int:
    agents = [a for a in pdb.get_agent_accounts() if a["agent_kind"] == "codex"]
    if not agents:
        return 0  # no codex agent account configured yet
    account_id = agents[0]["id"]

    files = list(_iter_session_files())
    if not files:
        return 0

    # One connection for the whole pass: rows AND state are committed together,
    # so a second connection never waits on this one's open write transaction.
    conn = pdb._connect()
    inserted = 0
    try:
        v1 = pdb._is_v1(conn)
        # Purge any previously imported rows for excluded models (idempotent —
        # only touches codex-imported rows, never real proxy traffic).
        if EXCLUDED_MODELS:
            placeholders = ",".join("?" * len(EXCLUDED_MODELS))
            conn.execute(
                f"DELETE FROM request_log WHERE model IN ({placeholders}) "
                "AND event_id LIKE 'codex:%'",
                tuple(EXCLUDED_MODELS),
            )
        if v1:
            cursor = conn.execute(
                "SELECT cursor_json FROM account_importers "
                "WHERE account_id=? AND importer_kind='codex' AND enabled=1",
                (account_id,),
            ).fetchone()
            try:
                raw_states = json.loads(cursor[0]) if cursor and cursor[0] else {}
            except (TypeError, ValueError):
                raw_states = {}
            states = raw_states if isinstance(raw_states, dict) else {}
        else:
            states = {
                row["path"]: dict(row) for row in conn.execute(
                    "SELECT path, size, mtime FROM codex_import_state"
                ).fetchall()
            }
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for path in files:
            spath = str(path)
            try:
                st = path.stat()
            except OSError:
                continue
            prev = states.get(spath)
            if prev is not None and prev["size"] == st.st_size and prev["mtime"] == int(st.st_mtime):
                continue  # unchanged since last pass

            line_count, rows = _parse_session(path)
            for row in rows:
                if pdb._insert_agent_usage_row(
                        conn, account_id, row["model"], row["prompt_tokens"],
                        row["completion_tokens"], row["cache_read_tokens"],
                        row["total_tokens"], row["requested_at"], row["event_id"]):
                    inserted += 1

            # Record scan progress.  Crash mid-file only costs a re-read of
            # that file next pass (INSERT OR IGNORE dedups regardless).
            state = {"size": st.st_size, "mtime": int(st.st_mtime),
                     "last_line": line_count, "session_id": _session_id_from_path(path),
                     "parsed_at": now_str}
            if v1:
                states[spath] = state
            else:
                conn.execute(
                    "INSERT INTO codex_import_state (path, size, mtime, last_line, session_id, parsed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "size=excluded.size,mtime=excluded.mtime,last_line=excluded.last_line,"
                    "session_id=excluded.session_id,parsed_at=excluded.parsed_at",
                    (spath, st.st_size, int(st.st_mtime), line_count,
                     state["session_id"], now_str),
                )
        if v1:
            conn.execute(
                "UPDATE account_importers SET cursor_json=? "
                "WHERE account_id=? AND importer_kind='codex' AND enabled=1",
                (json.dumps(states, ensure_ascii=False, separators=(",", ":")), account_id),
            )
        conn.commit()
    finally:
        conn.close()
    return inserted
