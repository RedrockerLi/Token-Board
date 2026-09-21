"""Merge physical Codex rollout segments into one logical transcript."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from app.services.agent_usage.common import timestamp


class CodexMergeCancelled(Exception):
    """Internal cancellation marker for a multi-file Codex source."""


def _open(path: Path):
    return (gzip.open(path, "rt", encoding="utf-8")
            if path.name.endswith(".gz") else
            path.open("r", encoding="utf-8"))


def _record_fingerprint(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def merge_codex_records(paths: tuple[Path, ...], stop_event=None) -> list[tuple[dict, dict]]:
    """Merge copied/continued rollouts without retaining chat text in state.

    Exact records are de-duplicated by occurrence number across physical
    copies, while repeated records within one file remain distinct. Edges from
    each file preserve its order; a topological merge then allows disjoint
    continuation files to interleave by timestamp. Conflicting order is a
    hard parse failure because choosing one order could double bill usage.
    """
    nodes = {}
    canonical_key = None
    ordinal = 0
    for file_index, path in enumerate(paths):
        occurrences = {}
        previous = None
        context = {}
        with _open(path) as stream:
            for raw in stream:
                if stop_event is not None and stop_event.is_set():
                    raise CodexMergeCancelled
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "turn_context":
                    payload = obj.get("payload")
                    if isinstance(payload, dict):
                        if isinstance(payload.get("model"), str):
                            context["model"] = payload["model"]
                        if "service_tier" in payload:
                            context["service_tier"] = payload.get("service_tier")
                elif (obj.get("type") == "event_msg"
                      and isinstance(obj.get("payload"), dict)
                      and obj["payload"].get("type") == "thread_settings_applied"):
                    settings = obj["payload"].get("thread_settings")
                    if isinstance(settings, dict):
                        if isinstance(settings.get("model"), str):
                            context["model"] = settings["model"]
                        if "service_tier" in settings:
                            context["service_tier"] = settings.get("service_tier")
                fingerprint = _record_fingerprint(obj)
                occurrence = occurrences.get(fingerprint, 0) + 1
                occurrences[fingerprint] = occurrence
                key = (fingerprint, occurrence)
                if file_index == 0 and canonical_key is None \
                        and obj.get("type") == "session_meta":
                    canonical_key = key
                if key not in nodes:
                    nodes[key] = {
                        "record": obj,
                        "context": dict(context),
                        "timestamp": timestamp(obj.get("timestamp")),
                        "ordinal": ordinal,
                        "successors": set(),
                        "incoming": 0,
                    }
                    ordinal += 1
                if previous is not None and previous != key:
                    node = nodes[previous]
                    if key not in node["successors"]:
                        node["successors"].add(key)
                        nodes[key]["incoming"] += 1
                previous = key

    ready = [key for key, node in nodes.items() if node["incoming"] == 0]

    def sort_key(key):
        node = nodes[key]
        if key == canonical_key:
            return (0, "", -1)
        value = node["timestamp"]
        return (1, value or "9999-12-31T23:59:59.999999Z", node["ordinal"])

    result = []
    while ready:
        ready.sort(key=sort_key)
        key = ready.pop(0)
        node = nodes[key]
        result.append((node["record"], node["context"]))
        for successor in node["successors"]:
            next_node = nodes[successor]
            next_node["incoming"] -= 1
            if next_node["incoming"] == 0:
                ready.append(successor)
    if len(result) != len(nodes):
        raise ValueError("Codex continuation copies have conflicting record order")
    return result


__all__ = ["CodexMergeCancelled", "merge_codex_records"]
