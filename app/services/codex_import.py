"""Codex session usage importer used by the dashboard server.

Parses Codex CLI session transcripts from ``~/.codex/sessions`` (recursively;
date-partitioned ``YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl``, plain JSONL;
``.jsonl.gz`` accepted for robustness) and inserts one ``request_log`` row per
``token_count`` event (per-turn granularity, using ``last_token_usage`` deltas),
attributed to the first ``agent_kind='codex'`` account.

Idempotency: each row's ``event_id`` is deterministic
(``codex:<session_id>:<line_no>``) and ``insert_agent_usage`` uses
``INSERT OR IGNORE``, so re-scanning a file — including after a crash — never
double-counts.  ``account_importers.cursor_json`` records ``(size, mtime)`` so
unchanged files are skipped entirely on later passes.

Failures propagate to the server scheduler, which records degraded health.
Browser requests only wake that scheduler and never execute a pass inline.
"""

import gzip
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

CODEX_DIR = Path.home() / ".codex" / "sessions"

# Models that must never be imported (noise from Codex sub-agents / auto-review
# turns).  Rows already imported for these models are deleted on every pass.
EXCLUDED_MODELS = {"codex-auto-review"}


def _iso_z_to_sqlite(ts: str) -> str | None:
    """'2026-08-04T16:37:44.757Z' → '2026-08-04T16:37:44Z' (ISO UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _token_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_id_from_path(path: Path) -> str:
    """Extract the session UUID from the filename, with fallbacks."""
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # rollout-<ts>-<uuid> → keep the trailing UUID; else the full stem.
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
        name,
    )
    if match:
        return match.group(1)
    return path.stem


def _open_maybe_gz(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


def _iter_session_files(stop_event: threading.Event | None = None):
    if not CODEX_DIR.is_dir():
        return
    seen = set()
    for pattern in ("*.jsonl", "*.jsonl.gz"):
        for p in CODEX_DIR.rglob(pattern):
            if stop_event is not None and stop_event.is_set():
                return
            if not p.is_file():
                continue
            key = p.resolve()
            if key not in seen:
                seen.add(key)
                yield p


def _parse_session(path: Path, stop_event: threading.Event | None = None):
    """Parse one session file → (total_line_count, [row dicts]).

    Each ``token_count`` event_msg yields one row using its ``last_token_usage``
    (per-turn delta).  Model is the last ``turn_context.payload.model`` seen.
    """
    rows = []
    session_id = None
    model = "codex"  # fallback when no turn_context carries a model
    line_no = 0
    with _open_maybe_gz(path) as source:
        for raw in source:
            if stop_event is not None and stop_event.is_set():
                return None
            line_no += 1
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            etype = obj.get("type")
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if etype == "session_meta":
                if payload.get("session_id"):
                    session_id = payload["session_id"]
            elif etype == "turn_context":
                m = payload.get("model")
                if isinstance(m, str) and m.strip():
                    model = m.strip()
            elif etype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if not isinstance(info, dict):
                    continue
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
                    "prompt_tokens": _token_int(last.get("input_tokens")),
                    "completion_tokens": _token_int(last.get("output_tokens")),
                    "cache_read_tokens": _token_int(last.get("cached_input_tokens")),
                    "total_tokens": _token_int(last.get("total_tokens")),
                    "requested_at": ts,
                    "event_id": event_id,
                })
    return line_no, rows


def import_once(pdb, stop_event: threading.Event | None = None) -> int:
    """Import one idempotent pass and return the number of inserted rows.

    Scheduling and retry policy belong to the server lifecycle worker.  This
    function intentionally lets failures propagate so that server health can
    report a degraded importer instead of silently claiming success.
    """
    if stop_event is not None and stop_event.is_set():
        return 0
    agents = [a for a in pdb.get_agent_accounts() if a["agent_kind"] == "codex"]
    if not agents:
        return 0  # no codex agent account configured yet
    account_id = agents[0]["id"]

    files = []
    for path in _iter_session_files(stop_event):
        files.append(path)
        if stop_event is not None and stop_event.is_set():
            return 0
    if not files:
        return 0
    # Parse each changed file outside a write transaction, then atomically
    # commit that file's rows and cursor.  A first scan may read many large
    # transcripts; it must not hold SQLite's single-writer lock while doing so.
    conn = pdb._connect()
    inserted = 0
    try:
        # Purge any previously imported rows for excluded models (idempotent —
        # only touches codex-imported rows, never real proxy traffic).
        if EXCLUDED_MODELS:
            placeholders = ",".join("?" * len(EXCLUDED_MODELS))
            conn.execute(
                f"DELETE FROM request_log WHERE model IN ({placeholders}) "
                "AND event_id LIKE 'codex:%'",
                tuple(EXCLUDED_MODELS),
            )
            conn.commit()
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
        for path in files:
            if stop_event is not None and stop_event.is_set():
                return inserted
            spath = str(path)
            try:
                before = path.stat()
            except OSError:
                continue
            prev = states.get(spath)
            same_mtime = (
                prev.get("mtime_ns") == int(before.st_mtime_ns)
                if isinstance(prev, dict) and "mtime_ns" in prev
                else isinstance(prev, dict)
                and prev.get("mtime") == int(before.st_mtime)
            )
            if (isinstance(prev, dict)
                    and prev.get("size") == before.st_size
                    and same_mtime):
                continue  # unchanged since last pass

            parsed = _parse_session(path, stop_event)
            if parsed is None:
                return inserted
            line_count, rows = parsed
            try:
                after = path.stat()
            except OSError:
                continue
            stable_snapshot = (
                after.st_size == before.st_size
                and int(after.st_mtime_ns) == int(before.st_mtime_ns)
            )

            file_inserted = 0
            try:
                # Claim the writer only after parsing.  Re-read the cursor
                # under that lock: during a rolling upgrade an old timer or a
                # second server may have committed another file since this
                # pass took its initial skip snapshot.
                conn.execute("BEGIN IMMEDIATE")
                for row in rows:
                    if stop_event is not None and stop_event.is_set():
                        conn.rollback()
                        return inserted
                    if pdb._insert_agent_usage_row(
                            conn, account_id, row["model"], row["prompt_tokens"],
                            row["completion_tokens"], row["cache_read_tokens"],
                            row["total_tokens"], row["requested_at"], row["event_id"]):
                        file_inserted += 1

                if stable_snapshot:
                    latest_cursor = conn.execute(
                        "SELECT cursor_json FROM account_importers "
                        "WHERE account_id=? AND importer_kind='codex' AND enabled=1",
                        (account_id,),
                    ).fetchone()
                    try:
                        latest_states = (json.loads(latest_cursor[0])
                                         if latest_cursor and latest_cursor[0]
                                         else {})
                    except (TypeError, ValueError):
                        latest_states = {}
                    if not isinstance(latest_states, dict):
                        latest_states = {}
                    latest_states[spath] = {
                        "size": after.st_size,
                        "mtime": int(after.st_mtime),
                        "mtime_ns": int(after.st_mtime_ns),
                        "last_line": line_count,
                        "session_id": _session_id_from_path(path),
                        "parsed_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"),
                    }
                    conn.execute(
                        "UPDATE account_importers SET cursor_json=? "
                        "WHERE account_id=? AND importer_kind='codex' AND enabled=1",
                        (json.dumps(latest_states, ensure_ascii=False,
                                    separators=(",", ":")), account_id),
                    )
                # If the live session grew during parsing, its complete rows
                # are still useful and safe to commit.  We deliberately leave
                # the cursor unchanged so the next pass re-reads and dedupes
                # the prefix before importing the appended events.
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            inserted += file_inserted
    finally:
        conn.close()
    return inserted
