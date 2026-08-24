"""Replay detection for Codex forked and sub-agent rollouts.

Codex may serialize part of a parent transcript into a new rollout.  The
normal usage parser should still count the child, but inherited token_count
records must not be counted a second time.  This module keeps that concern
out of the usage-event adapter and stores only compact fingerprints in memory.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path


def _timestamp_ms(value) -> float | None:
    """Return an ISO or epoch timestamp as milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value * 1000 if value < 1_000_000_000_000 else value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            numeric = None
        if numeric is not None:
            return float(numeric * 1000 if numeric < 1_000_000_000_000 else numeric)
        try:
            return datetime.fromisoformat(
                stripped.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return None
    return None


def _session_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name


def _open_maybe_gz(path: Path):
    return (gzip.open(path, "rt", encoding="utf-8")
            if path.name.endswith(".gz") else
            open(path, "r", encoding="utf-8"))


def _json_fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_index(path: Path,
                   stop_event: threading.Event | None = None) -> dict:
    session_id = None
    forked_from_id = None
    parent_thread_id = None
    session_started_at = None
    is_subagent = False
    session_meta_count = 0
    token_fingerprints = []
    token_times = []
    task_boundaries = []
    raw_token_count = 0
    line_no = 0
    logical_timestamp = None
    pending_token_times = []
    with _open_maybe_gz(path) as source:
        for raw in source:
            if stop_event is not None and stop_event.is_set():
                return {"cancelled": True}
            line_no += 1
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            record_timestamp = _timestamp_ms(obj.get("timestamp"))
            if record_timestamp is not None:
                logical_timestamp = max(
                    logical_timestamp if logical_timestamp is not None else record_timestamp,
                    record_timestamp,
                )
                for index in pending_token_times:
                    token_times[index] = logical_timestamp
                pending_token_times = []
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                session_meta_count += 1
                if session_meta_count == 1:
                    meta = obj["payload"]
                    session_id = meta.get("id") or meta.get("session_id")
                    forked_from_id = meta.get("forked_from_id")
                    source_meta = meta.get("source")
                    if isinstance(source_meta, dict):
                        subagent_meta = source_meta.get("subagent")
                        thread_spawn = (
                            subagent_meta.get("thread_spawn")
                            if isinstance(subagent_meta, dict)
                            else None
                        )
                        parent_thread_id = (
                            meta.get("parent_thread_id")
                            or source_meta.get("parent_thread_id")
                            or (thread_spawn.get("parent_thread_id")
                                if isinstance(thread_spawn, dict) else None)
                        )
                    else:
                        parent_thread_id = meta.get("parent_thread_id")
                    is_subagent = (
                        meta.get("thread_source") == "subagent"
                        or source_meta == "subagent"
                        or (isinstance(source_meta, dict) and "subagent" in source_meta)
                        or parent_thread_id is not None
                    )
                    session_started_at = _timestamp_ms(
                        meta.get("timestamp") or obj.get("timestamp"))
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if obj.get("type") == "event_msg" and payload.get("type") == "token_count":
                raw_token_count += 1
                token_fingerprints.append(_json_fingerprint(payload))
                if record_timestamp is None:
                    token_times.append(float("inf"))
                    pending_token_times.append(len(token_times) - 1)
                else:
                    token_times.append(logical_timestamp)
            elif obj.get("type") == "event_msg" and payload.get("type") in {
                    "task_started", "turn_started"}:
                task_boundaries.append({
                    "raw_token_count": raw_token_count,
                    "started_at": _timestamp_ms(payload.get("started_at")),
                    "line_no": line_no,
                })
    return {
        "path": path,
        "session_id": session_id or _session_id_from_path(path),
        "forked_from_id": forked_from_id,
        "parent_thread_id": parent_thread_id,
        "is_subagent": is_subagent,
        "session_meta_count": session_meta_count,
        "session_started_at": session_started_at,
        "token_fingerprints": token_fingerprints,
        "token_times": token_times,
        "task_boundaries": task_boundaries,
        "raw_token_count": raw_token_count,
    }


def _longest_prefix_suffix(child: list[str], parent: list[str]) -> int:
    limit = min(len(child), len(parent))
    for size in range(limit, 0, -1):
        if child[:size] == parent[-size:]:
            return size
    return 0


def _longest_prefix_inside(child: list[str], parent: list[str]) -> int:
    for size in range(min(len(child), len(parent)), 0, -1):
        prefix = child[:size]
        if any(parent[index:index + size] == prefix
               for index in range(len(parent) - size + 1)):
            return size
    return 0


def replay_skip_counts(files,
                       stop_event: threading.Event | None = None) -> dict[str, int]:
    """Return the raw token_count prefix to skip for each replaying file."""
    indexes = []
    for path in files:
        item = _session_index(path, stop_event)
        if item.get("cancelled"):
            return {}
        indexes.append(item)

    # During live/archive moves the same logical session can exist twice.
    # Prefer the most complete copy as the parent snapshot.
    by_session = {}
    for item in indexes:
        current = by_session.get(item["session_id"])
        try:
            size = item["path"].stat().st_size
        except OSError:
            size = 0
        if current is None or size > current[0]:
            by_session[item["session_id"]] = (size, item)

    result = {}
    for item in indexes:
        parent_id = item["forked_from_id"] or (
            item["parent_thread_id"] if item["is_subagent"] else None)
        parent = by_session.get(parent_id, (0, None))[1] if parent_id else None
        parent_tokens = []
        if parent is not None:
            if item["session_started_at"] is not None:
                parent_tokens = [
                    fingerprint for fingerprint, timestamp in zip(
                        parent["token_fingerprints"], parent["token_times"])
                    if timestamp <= item["session_started_at"]
                ]
            else:
                parent_tokens = parent["token_fingerprints"]
        child_tokens = item["token_fingerprints"]
        skipped = _longest_prefix_suffix(child_tokens, parent_tokens)
        if item["is_subagent"]:
            skipped = max(skipped,
                          _longest_prefix_inside(child_tokens, parent_tokens))
            matching = [
                boundary for boundary in item["task_boundaries"]
                if boundary["raw_token_count"] == skipped
                and boundary.get("started_at") is not None
                and (item["session_started_at"] is None
                     or boundary["started_at"] >= item["session_started_at"])
            ]
            own_task = [
                boundary for boundary in item["task_boundaries"]
                if boundary.get("started_at") is not None
                and item["session_started_at"] is not None
                and abs(boundary["started_at"] - item["session_started_at"]) <= 5_000
            ]
            if matching:
                skipped = max(skipped, matching[-1]["raw_token_count"])
            elif own_task:
                skipped = max(skipped, own_task[-1]["raw_token_count"])
            elif parent is None and item["session_meta_count"] == 1:
                skipped = max(skipped, max(
                    (boundary["raw_token_count"]
                     for boundary in item["task_boundaries"]),
                    default=0))
        if skipped:
            result[str(item["path"])] = skipped
    return result
