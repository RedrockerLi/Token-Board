"""Claude Code / Claude Desktop Code JSONL adapter."""

from __future__ import annotations

import os
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    estimated_latency_ms,
    iter_jsonl,
    make_event,
    project_name,
    safe_int,
    source,
    timestamp,
    timestamp_ms,
    walk_files,
)
from ..ir import ParseBatch, UsageSource

KIND = "claude-code"
LABEL = "Claude Code"
DEFAULT_PATH = Path.home() / ".claude"
log = logging.getLogger(__name__)

# 沿父链往上最多走这么多跳(与 ref/cc-switch 一致)。
MAX_CHAIN_HOPS = 32


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        roots = [Path(configured).expanduser()]
    else:
        override = os.environ.get("VIBE_USAGE_CLAUDE_DIRS", "").strip()
        if override:
            roots = [Path(value).expanduser()
                     for value in override.split(os.pathsep) if value]
        else:
            roots = [Path.home() / ".claude"]
            explicit = os.environ.get("CLAUDE_CONFIG_DIR")
            if explicit:
                roots.append(Path(explicit).expanduser())
            try:
                roots.extend(path for path in Path.home().glob(".claude-*")
                             if path.is_dir())
            except OSError:
                log.debug("Claude Code profile roots are unavailable", exc_info=True)
            desktop_override = os.environ.get("VIBE_USAGE_CLAUDE_DESKTOP_DIRS", "").strip()
            if desktop_override:
                desktop_dirs = [Path(value).expanduser()
                                for value in desktop_override.split(os.pathsep) if value]
            elif sys.platform == "darwin":
                desktop_dirs = [Path.home() / "Library" / "Application Support" / "Claude"]
            elif sys.platform == "win32":
                desktop_dirs = [Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"]
            else:
                desktop_dirs = [Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"]
            for desktop_dir in desktop_dirs:
                roots.extend(_desktop_session_roots(
                    desktop_dir / "local-agent-mode-sessions"))

    # Match the reference parser's additive extra-root behavior.  A configured
    # data_root remains the primary root; explicit extra roots are useful for
    # another Claude profile or a relocated/foreign OS store.
    roots.extend(configured_extra_roots(software, KIND))
    return list(dict.fromkeys(roots))


def _desktop_session_roots(directory: Path, depth: int = 0) -> list[Path]:
    if depth > 8:
        return []
    try:
        children = list(directory.iterdir())
    except OSError:
        return []
    result = []
    for child in children:
        if not child.is_dir():
            continue
        if child.name == ".claude":
            result.append(child)
            continue
        if child.name in {"rpm", "skills"}:
            continue
        result.extend(_desktop_session_roots(child, depth + 1))
    return result


def _relative_project(path: Path, directory: Path) -> str:
    try:
        relative = path.relative_to(directory)
        first = relative.parts[0] if relative.parts else ""
    except ValueError:
        first = ""
    parts = [part for part in first.split("-") if part]
    return parts[-1] if parts else "unknown"


def _cache_creation_tokens(usage: dict) -> int:
    """Return cache-write tokens for the legacy request-log projection.

    The reference parser now keeps the 5-minute and 1-hour TTL buckets
    separately.  Token-Board's request_log has no cache-creation column (the
    proxy's UsageAccounting compatibility rule deliberately folds writes into
    prompt tokens), so preserve the exact old total here while keeping the
    cache-read bucket independent for hit/miss reporting.
    """
    breakdown = usage.get("cache_creation")
    if not isinstance(breakdown, dict):
        breakdown = {}
    return max(
        safe_int(usage.get("cache_creation_input_tokens")),
        safe_int(breakdown.get("ephemeral_5m_input_tokens"))
        + safe_int(breakdown.get("ephemeral_1h_input_tokens")),
    )


def _is_fast_mode(usage: dict, message: dict) -> bool:
    speed = usage.get("speed")
    if speed is None:
        speed = message.get("speed")
    return isinstance(speed, str) and speed.strip().lower() == "fast"


def _apply_speed_marker(model: str, fast: bool) -> str:
    if not fast or not model:
        return model
    return model if model.endswith("-fast") else f"{model}-fast"


@dataclass
class _MessageTiming:
    """一条回复(同一 message.id 的全部内容块)的计时线索。"""

    # 文件里最先出现的那一块的父行,请求起点沿它往上找。
    first_parent: str | None
    # 最先出现的那一块是不是这条回复的第一块。不是的话起点会取晚,宁可不估。
    starts_at_first_block: bool
    # 各块时间戳(块写完的时刻)里最晚的一个,即回复结束。
    end_ms: int | None = None
    # 这个 message.id 的行里是否出现过非空 stop_reason(回复写完整了)。
    has_stop_reason: bool = False


def _line_uuid(obj: dict) -> str | None:
    value = obj.get("uuid")
    return value if isinstance(value, str) and value else None


def _parent_uuid(obj: dict) -> str | None:
    value = obj.get("parentUuid")
    return value if isinstance(value, str) and value else None


def _record_chain_node(chain: dict, obj: dict) -> None:
    """把一行日志记入对话链:uuid → (parentUuid, timestamp_ms, is_attachment)。

    attachment 行的时间戳不可靠(有的是回复开始后才补记的),找请求起点时
    要跳过,所以单独标记。
    """
    uuid = _line_uuid(obj)
    if uuid is None:
        return
    chain[uuid] = (_parent_uuid(obj), timestamp_ms(obj.get("timestamp")),
                   obj.get("type") == "attachment")


def _assistant_message_id(obj: dict) -> str | None:
    """assistant 行的 message.id;别的行返回 None。"""
    if obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    return value if isinstance(value, str) else None


def _record_block_timing(timings: dict, obj: dict, msg_id: str) -> None:
    """把一个内容块记入它所属回复的计时:最先出现的块定起点,最晚的块定结束。"""
    end_ms = timestamp_ms(obj.get("timestamp"))
    timing = timings.get(msg_id)
    if timing is None:
        index = obj.get("apiBlockIndex")
        timing = _MessageTiming(
            first_parent=_parent_uuid(obj),
            # apiBlockIndex 缺省(旧版日志)按第一块处理。
            starts_at_first_block=index is None or (
                isinstance(index, int) and not isinstance(index, bool)
                and index == 0),
        )
        timings[msg_id] = timing
    if end_ms is not None and (timing.end_ms is None or end_ms > timing.end_ms):
        timing.end_ms = end_ms
    message = obj.get("message")
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    if isinstance(stop_reason, str) and stop_reason:
        timing.has_stop_reason = True


def _resolve_request_start_ms(chain: dict, first_parent: str | None) -> int | None:
    """请求起点:从回复第一块的父行沿链上溯,跳过 attachment,取第一个别的行的时间戳。"""
    cursor = first_parent
    if cursor is None:
        return None
    for _ in range(MAX_CHAIN_HOPS):
        node = chain.get(cursor)
        if node is None:
            return None
        parent, node_ms, is_attachment = node
        if not is_attachment:
            return node_ms
        cursor = parent
        if cursor is None:
            return None
    return None


def _message_durations(chain: dict, timings: dict) -> dict:
    """按 message.id 给出可入库的估算耗时;估不出的不出现(入库按 0 处理)。

    只给写完整了的回复算(出现过 stop_reason),而且要看得到它的第一块,
    否则起点会对不上(与 ref/cc-switch 同口径)。
    """
    durations = {}
    for msg_id, timing in timings.items():
        if not timing.has_stop_reason or not timing.starts_at_first_block:
            continue
        if timing.end_ms is None:
            continue
        start_ms = _resolve_request_start_ms(chain, timing.first_parent)
        if start_ms is None:
            continue
        latency = estimated_latency_ms(start_ms, timing.end_ms)
        if latency is not None:
            durations[msg_id] = latency
    return durations


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    roots = _roots(software)
    extra_roots = configured_extra_roots(software, KIND)
    discovery_warnings = []
    for extra in extra_roots:
        if not extra.is_dir() or not any(
                (extra / directory).is_dir()
                for directory in ("projects", "transcripts")):
            discovery_warnings.append(
                f"claude-code: 额外数据根目录不可用，已保留上次同步状态: {extra}"
            )
    for root in roots:
        for directory in ("projects", "transcripts"):
            base = root / directory
            for path in walk_files(base, (".jsonl",)):
                key = (path.stem, directory == "projects")
                try:
                    stat = path.stat()
                except OSError:
                    continue
                current = candidates.get(key)
                if current is None or (stat.st_size, stat.st_mtime_ns) > current[0]:
                    candidates[key] = ((stat.st_size, stat.st_mtime_ns), path, directory, _relative_project(path, base))
    # A project copy is authoritative when both project and transcript copies
    # exist for the same session id.
    selected = {}
    for (_, is_project), (_, path, directory, fallback_project) in candidates.items():
        session = path.stem
        if session not in selected or is_project:
            selected[session] = (path, directory, fallback_project)
    if discovery_warnings:
        warning_path = extra_roots[0] if extra_roots else DEFAULT_PATH
        return [source(
            warning_path,
            key="claude-code-discovery",
            discovery_warnings=tuple(dict.fromkeys(discovery_warnings)),
        )]
    return [source(path, project_file=directory == "projects", fallback_project=fallback_project)
            for path, directory, fallback_project in selected.values()]


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    # 估算耗时用的对话链与回复计时:整个文件先扫一遍(含没有 usage 的行)。
    chain = {}
    timings = {}
    for _line_no, obj in iter_jsonl(item.path):
        _record_chain_node(chain, obj)
        msg_id = _assistant_message_id(obj)
        if msg_id is not None:
            _record_block_timing(timings, obj, msg_id)
    durations = _message_durations(chain, timings)

    events = []
    count = 0
    first_project = item.context.get("fallback_project") or "unknown"
    found_cwd = False
    last_model = None
    seen = {}
    for line_no, obj in iter_jsonl(item.path):
        count = line_no
        if not found_cwd and obj.get("cwd"):
            first_project = project_name(obj.get("cwd"))
            found_cwd = True
        if obj.get("type") != "assistant" or not isinstance(obj.get("message"), dict):
            continue
        usage = obj["message"].get("usage")
        if not isinstance(usage, dict):
            continue
        ts = timestamp(obj.get("timestamp"))
        msg_id = _assistant_message_id(obj)
        call_id = f"{obj.get('message', {}).get('id', '')}\0{obj.get('requestId', '')}".strip("\0") or str(obj.get("uuid") or line_no)
        raw_model = obj["message"].get("model")
        if isinstance(raw_model, str) and raw_model.strip() and raw_model != "<synthetic>":
            last_model = raw_model.strip()
        base_model = last_model or "claude-unknown"
        model = _apply_speed_marker(base_model, _is_fast_mode(usage, obj["message"]))
        cache_creation = _cache_creation_tokens(usage)
        event = make_event(
            kind=KIND, source_key="call", ordinal=call_id,
            model=model,
            requested_at=ts,
            input_tokens=safe_int(usage.get("input_tokens")) + cache_creation,
            output_tokens=usage.get("output_tokens", 0), cached_input_tokens=usage.get("cache_read_input_tokens", 0),
            project=first_project or "unknown", session_id=item.path.stem,
            # 同一 message.id 的各块共用一份估算耗时,去重后落在保留的那一条上。
            duration_ms=durations.get(msg_id, 0),
        )
        if event:
            current = seen.get(call_id)
            score = event.prompt_tokens + event.completion_tokens
            if current is None or score > current[0]:
                seen[call_id] = (score, event)
    events.extend(value[1] for value in seen.values())
    return batch(events, count)
