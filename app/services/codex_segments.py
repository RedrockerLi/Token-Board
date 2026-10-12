"""Merge physical Codex rollout segments into one logical transcript."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from app.services.agent_usage.common import timestamp

# 归一化后保留的用量字段;非有限数(含缺失/null)不保留。
_USAGE_RECORD_FIELDS = (
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_read_input_tokens", "cache_write_input_tokens",
    "reasoning_output_tokens", "total_tokens",
)


class CodexMergeCancelled(Exception):
    """Internal cancellation marker for a multi-file Codex source."""


def _open(path: Path):
    return (gzip.open(path, "rt", encoding="utf-8")
            if path.name.endswith(".gz") else
            path.open("r", encoding="utf-8"))


def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _pick_usage(value: dict) -> dict:
    return {key: value[key] for key in _USAGE_RECORD_FIELDS
            if key in value and _is_finite_number(value[key])}


def normalize_usage_record(obj: Any) -> Any:
    """把 durable ``token_usage_record`` 归一成 UI ``token_count`` 形状。

    Codex 对同一次请求写两遍用量:一遍是 UI 的 ``event_msg``/``token_count``,
    一遍是 durable 的 ``token_usage_record``。中断或崩溃的调用可能只留下后者,
    只数 UI 事件会漏报。归一后 ``payload.info.last_token_usage`` 是本次请求的
    用量、``total_token_usage`` 是线程累计用量,并保留 ``usage_record`` 标记,
    供解析器与 UI mirror 配对。畸形记录(负值、非有限数、累计用量非正)原样
    返回,按未知记录忽略。
    """
    if not isinstance(obj, dict) or obj.get("type") != "token_usage_record":
        return obj
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return obj
    usage = payload.get("usage")
    total = payload.get("thread_token_usage")
    if not isinstance(usage, dict) or not isinstance(total, dict):
        return obj
    total_tokens = total.get("total_tokens")
    if not _is_finite_number(total_tokens) or total_tokens <= 0:
        return obj
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if not _is_finite_number(value) or value < 0:
            return obj
    for key in ("cached_input_tokens", "reasoning_output_tokens"):
        value = usage.get(key)
        if value is None:
            value = 0
        if not _is_finite_number(value) or value < 0:
            return obj
    return {
        **obj,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "usage_record": True,
            "info": {
                "last_token_usage": _pick_usage(usage),
                "total_token_usage": _pick_usage(total),
            },
        },
    }


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
    Durable usage records are normalized before they enter the graph so their
    ``usage_record`` marker survives the merge.
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
                # durable 记录先归一成 UI token_count 形状,再取指纹并入图:
                # 选中的负载带着 usage_record 标记,与 UI 事件走同一套去重与
                # 计数规则(对应 ref 的 codex-segments.js)。
                record = normalize_usage_record(obj)
                fingerprint = _record_fingerprint(record)
                occurrence = occurrences.get(fingerprint, 0) + 1
                occurrences[fingerprint] = occurrence
                key = (fingerprint, occurrence)
                if file_index == 0 and canonical_key is None \
                        and record.get("type") == "session_meta":
                    canonical_key = key
                if key not in nodes:
                    nodes[key] = {
                        "record": record,
                        "context": dict(context),
                        "timestamp": timestamp(record.get("timestamp")),
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


__all__ = ["CodexMergeCancelled", "merge_codex_records", "normalize_usage_record"]
