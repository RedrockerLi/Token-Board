"""Codex CLI rollout adapter.

Codex is the one adapter with lineage-aware replay handling.  The parser keeps
the existing duplicate/cumulative semantics while emitting ``UsageEvent``
objects instead of raw dictionaries.

Codex writes the same usage twice: as the durable ``token_usage_record`` and as
the UI-facing ``event_msg``/``token_count``.  Durable records are normalized
into the UI shape so one loop handles both, and the pair is matched on
per-request usage because the two cumulative counters diverge after an
interrupted call.  The UI event keeps the identity the legacy parser gave it
(its line for a single file, its position in the merged UI stream otherwise),
while a durable record that never got a UI event is imported under a
``record:<n>`` ordinal, so upgrading never re-imports an existing row.
``RequestTimer`` estimates a per-request duration from the rollout timestamps;
Codex logs carry no request-level timing of their own.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    estimated_latency_ms,
    event_id_for,
    make_event,
    project_name,
    safe_int,
    source,
    timestamp,
    timestamp_ms,
)
from ..ir import ParseBatch, UsageEvent, UsageSource
from ..cindy_ledger import discover as discover_cindy, parse as parse_cindy
from app.services.codex_replay import replay_skip_counts
from app.services.codex_segments import (
    CodexMergeCancelled,
    merge_codex_records,
    normalize_usage_record,
)

KIND = "codex"
LABEL = "Codex"
DESCRIPTION = "Codex CLI 会话用量"
DEFAULT_PATH_DISPLAY = "~/.codex"
CODEX_HOME = Path.home() / ".codex"
DEFAULT_PATH = CODEX_HOME
SERVICE_TIER_ATTRIBUTION_START = datetime(2026, 8, 31, tzinfo=timezone.utc)
ALWAYS_SCAN = True


def _service_tier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in {"fast", "priority", "flex", "batch"} else None


def _decorate_model(model: str, service_tier: str | None,
                    requested_at: str | None) -> str:
    if not service_tier or not requested_at or model == "unknown":
        return model
    try:
        parsed = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return model
    if parsed < SERVICE_TIER_ATTRIBUTION_START:
        return model
    return f"{model}-{service_tier}"


def _is_codex_home(path: Path) -> bool:
    return path.is_dir() and any(
        (path / name).is_dir() for name in ("sessions", "archived_sessions")
    )


def _discover_codex_homes(root: Path, max_depth: int = 3) -> list[Path]:
    """Resolve a Codex home or bounded Multica container into Codex homes."""
    root = root.expanduser()
    if _is_codex_home(root):
        return [root]
    if not root.is_dir():
        return []
    homes = []
    queue = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_symlink() or not child.is_dir():
                    continue
            except OSError:
                continue
            child_depth = depth + 1
            if child.name == "codex-home" and _is_codex_home(child):
                homes.append(child)
                continue
            if child_depth < max_depth:
                queue.append((child, child_depth))
    result = []
    seen = set()
    for home in homes:
        try:
            key = home.resolve()
        except OSError:
            key = home
        if key not in seen:
            seen.add(key)
            result.append(home)
    return result


def _homes_for_root(root: Path) -> list[Path]:
    root = root.expanduser()
    if root.name in {"sessions", "archived_sessions"}:
        return [root.parent] if _is_codex_home(root.parent) else []
    return _discover_codex_homes(root)


def _roots(software: dict) -> list[Path]:
    configured = config_value(software, "data_root", "path")
    if configured:
        root = Path(configured).expanduser()
        homes = [root.parent if root.name in {"sessions", "archived_sessions"}
                 else root]
    else:
        homes = [Path(os.environ.get("CODEX_HOME", str(CODEX_HOME))).expanduser()]
    extra = config_value(
        software, "codex_extra_home", "extra_codex_home", "extra_data_root",
    ) or os.environ.get("VIBE_USAGE_CODEX_EXTRA_HOME") or os.environ.get("CODEX_EXTRA_HOME")
    if extra:
        homes.extend(_homes_for_root(Path(extra)))
    for root in configured_extra_roots(software, KIND):
        homes.extend(_homes_for_root(root))
    unique_homes = []
    seen = set()
    for home in homes:
        try:
            key = home.resolve()
        except OSError:
            key = home
        if key not in seen:
            seen.add(key)
            unique_homes.append(home)
    return [home / directory for home in unique_homes
            for directory in ("sessions", "archived_sessions")]


def _session_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$", name)
    return match.group(1) if match else path.stem


def _session_id_for_path(path: Path) -> str:
    """Prefer the durable header id when a rollout name is non-canonical."""
    try:
        with _open(path) as stream:
            for index, raw in enumerate(stream):
                if index >= 256:
                    break
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if isinstance(payload, dict):
                    value = payload.get("id") or payload.get("session_id")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                break
    except (OSError, EOFError, gzip.BadGzipFile):
        return _session_id_from_path(path)
    return _session_id_from_path(path)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    candidates = {}
    seen = set()
    discovery_warnings = []
    configured_extra = configured_extra_roots(software, KIND)
    for extra in configured_extra:
        if not _homes_for_root(extra):
            discovery_warnings.append(
                f"codex: 额外数据根目录不可用，已保留上次同步状态: {extra}"
            )
    for root in _roots(software):
        if stop_event is not None and stop_event.is_set():
            break
        if not root.is_dir():
            continue
        for pattern in ("*.jsonl", "*.jsonl.gz"):
            if stop_event is not None and stop_event.is_set():
                break
            try:
                paths = root.rglob(pattern)
            except OSError:
                continue
            for path in paths:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    key = path.resolve()
                except OSError:
                    key = path
                if path.is_file() and key not in seen:
                    seen.add(key)
                    session_id = _session_id_for_path(path)
                    try:
                        stat = path.stat()
                        score = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        score = (0, 0)
                    candidates.setdefault(session_id, []).append((score, path))
            if stop_event is not None and stop_event.is_set():
                break
    out = []
    for session_id, members in candidates.items():
        members.sort(key=lambda value: str(value[1]))
        representative = max(members, key=lambda value: value[0])[1]
        out.append(source(
            representative,
            session_id=session_id,
            segments=tuple(path for _, path in members),
        ))
    if discovery_warnings:
        warning_path = configured_extra[0] if configured_extra else CODEX_HOME
        return [source(
            warning_path,
            key="codex-discovery",
            discovery_warnings=tuple(dict.fromkeys(discovery_warnings)),
        )]
    out.extend(discover_cindy(software, KIND))
    return out


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.name.endswith(".gz") else path.open("r", encoding="utf-8")


def _records_for_item(item: UsageSource, stop_event=None):
    segments = item.context.get("segments")
    if not isinstance(segments, (list, tuple)) or not segments:
        segments = (item.path,)
    paths = tuple(Path(path) for path in segments)
    if len(paths) > 1:
        for index, (record, context) in enumerate(
                merge_codex_records(paths, stop_event), 1):
            yield record, context, index
        return
    with _open(paths[0]) as stream:
        for line_no, raw in enumerate(stream, 1):
            if stop_event is not None and stop_event.is_set():
                raise CodexMergeCancelled
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                yield None, {}, line_no
                continue
            yield record, {}, line_no


# 两行的时间戳相差不超过这个毫秒数,就算同一批写出的(实测相差 0–1 毫秒,而一次
# 真实的请求不可能这么快);与 ref/cc-switch 一致。
SAME_FLUSH_SLACK_MS = 100

_TIMING_BOUNDARY = "boundary"
_TIMING_TOOL_OUTPUT = "tool_output"
_TIMING_MODEL_OUTPUT = "model_output"
_TIMING_USAGE_RECORD = "usage_record"
_TIMING_TURN_RESET = "turn_reset"

# 计时用的累计用量字段(与 ref/cc-switch 的 parse_cumulative_tokens 同口径)。
_TIMER_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_read_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def _is_task_started(payload: dict) -> bool:
    """task_started/turn_started 开始新一轮;pending mirror 标记不跨轮保留。"""
    return payload.get("type") in ("task_started", "turn_started")


def _unsigned_int(value: Any) -> int:
    """按无符号整数口径取数:浮点、字符串、负数、布尔都记 0。"""
    return (value if isinstance(value, int) and not isinstance(value, bool)
            and value >= 0 else 0)


def _cumulative_tokens(value: Any) -> dict[str, int] | None:
    """提取计时用的累计用量;不含任何已知字段时返回 None。"""
    if not isinstance(value, dict):
        return None
    if not any(key in value for key in _TIMER_USAGE_FIELDS):
        return None
    return {
        "input": _unsigned_int(value.get("input_tokens")),
        "cached_input": _unsigned_int(value.get("cached_input_tokens")),
        "output": _unsigned_int(value.get("output_tokens")),
    }


def _same_request_usage(left: Any, right: Any) -> bool:
    """两条用量是否描述同一次请求(input/cached/output/reasoning 全等)。"""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = ("input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens")
    return all((left.get(key) or 0) == (right.get(key) or 0) for key in keys)


def _classify_timing_record(record: dict) -> str | None:
    """按一行日志在对话链上的位置分类(与 ref/cc-switch 同口径)。"""
    record_type = record.get("type")
    payload = record.get("payload")
    if record_type == "turn_context":
        return _TIMING_BOUNDARY
    if record_type == "token_usage_record":
        return _TIMING_USAGE_RECORD
    if record_type == "response_item":
        if not isinstance(payload, dict):
            return None
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            if not isinstance(role, str):
                return None
            return (_TIMING_MODEL_OUTPUT if role == "assistant"
                    else _TIMING_BOUNDARY)
        if item_type == "reasoning":
            return _TIMING_MODEL_OUTPUT
        if isinstance(item_type, str) and item_type.endswith("_output"):
            return _TIMING_TOOL_OUTPUT
        if isinstance(item_type, str) and item_type.endswith("_call"):
            return _TIMING_MODEL_OUTPUT
        return None
    if record_type == "event_msg":
        if not isinstance(payload, dict):
            return None
        # 多段合并会把 durable 记录先归一成 UI 形状,这里按 usage_record 标记
        # 认出它仍然是一次响应结束时的用量记录。
        if payload.get("usage_record") is True:
            return _TIMING_USAGE_RECORD
        if payload.get("type") in ("task_started", "turn_aborted"):
            return _TIMING_TURN_RESET
        return None
    return None


def _later(current: int | None, at: int | None) -> int | None:
    """两个时间戳取较晚的一个。"""
    if at is None:
        return current
    return at if current is None else max(current, at)


class RequestTimer:
    """按日志时间戳估算每次请求的耗时(移植 ref/cc-switch 的 RequestTimer)。

    行分类见 :func:`_classify_timing_record`,计时口径:

    - 结束时刻:新版 Codex 在响应结束时写 ``token_usage_record``,直接用它;
      旧版只有 ``token_count``,有的版本在响应结束时写,有的要等工具跑完才写。
      后一种表现为「最后一个输出项之后先出现工具结果,``token_count`` 紧跟着
      工具结果写出」,这时止于最后一个输出项的时刻(工具执行不算生成时间)。
    - 开始时刻:本次请求出现第一个输出项时,取它之前最近的一个起点行(上一次的
      ``token_count``、工具结果、用户消息、``turn_context``)。不取结束前最近的
      起点行,因为工具结果可能在响应还没结束时就写进来了。
    - 旧布局把上一次响应的输出项补写在它的 ``token_count`` 之后(时间戳与那个
      ``token_count`` 相同),这些输出项被忽略,否则下一次请求的起点会被钉在
      上一次响应结束的时刻。
    - ``task_started``/``turn_aborted`` 会清掉等待中的请求起点,免得被下一次
      请求沿用;用户消息不清,因为响应中途会插入 developer 消息。
    """

    def __init__(self) -> None:
        self.last_boundary_ms: int | None = None
        self.last_token_count_ms: int | None = None
        self.request_start_ms: int | None = None
        self.last_model_output_ms: int | None = None
        # 最后一个输出项之后出现的工具结果里最晚的一个;之后再有输出项就清空。
        self.tool_output_after_model_output_ms: int | None = None
        self.usage_record: tuple[int, dict[str, int]] | None = None

    def observe(self, record: dict) -> None:
        """观察一行日志,推进计时状态。"""
        kind = _classify_timing_record(record)
        if kind is None:
            return
        at = timestamp_ms(record.get("timestamp"))
        if at is None:
            return
        if kind == _TIMING_BOUNDARY:
            self.last_boundary_ms = _later(self.last_boundary_ms, at)
        elif kind == _TIMING_TOOL_OUTPUT:
            self.last_boundary_ms = _later(self.last_boundary_ms, at)
            if self.last_model_output_ms is not None:
                self.tool_output_after_model_output_ms = at
        elif kind == _TIMING_MODEL_OUTPUT:
            trails_token_count = (
                self.request_start_ms is None
                and self.last_token_count_ms is not None
                and at - self.last_token_count_ms <= SAME_FLUSH_SLACK_MS)
            if trails_token_count:
                return
            if self.request_start_ms is None:
                self.request_start_ms = self.last_boundary_ms
            self.last_model_output_ms = at
            self.tool_output_after_model_output_ms = None
        elif kind == _TIMING_USAGE_RECORD:
            payload = record.get("payload")
            usage = (_cumulative_tokens(payload.get("usage"))
                     if isinstance(payload, dict) else None)
            if usage is None and isinstance(payload, dict):
                # 归一后的形状:本次请求的用量在 info.last_token_usage
                info = payload.get("info")
                if isinstance(info, dict):
                    usage = _cumulative_tokens(info.get("last_token_usage"))
            self.usage_record = (at, usage) if usage is not None else None
        elif kind == _TIMING_TURN_RESET:
            self.last_boundary_ms = _later(self.last_boundary_ms, at)
            self.request_start_ms = None
            self.last_model_output_ms = None
            self.tool_output_after_model_output_ms = None
            self.usage_record = None

    def finish_request(self, token_count_ms: int | None,
                       last: dict[str, int] | None) -> int | None:
        """结算一次有用量的请求:返回估算耗时,并把状态推进到本次结束。

        ``last`` 是这次请求自己的用量,用来确认待取的 ``token_usage_record``
        说的是同一次请求。区间外的耗时由 ``estimated_latency_ms`` 判为 None。
        """
        start_ms = (self.request_start_ms if self.request_start_ms is not None
                    else self.last_boundary_ms)
        record_end_ms = None
        if self.usage_record is not None:
            record_at, record_usage = self.usage_record
            if last is None or (
                    record_usage["input"] == last["input"]
                    and record_usage["cached_input"] == last["cached_input"]
                    and record_usage["output"] == last["output"]):
                record_end_ms = record_at
        waited_for_tools = False
        if self.tool_output_after_model_output_ms is not None:
            waited_for_tools = (
                token_count_ms is None
                or token_count_ms - self.tool_output_after_model_output_ms
                <= SAME_FLUSH_SLACK_MS)
        end_ms = record_end_ms
        if end_ms is None:
            end_ms = (self.last_model_output_ms if waited_for_tools
                      else token_count_ms)
        self.last_boundary_ms = _later(self.last_boundary_ms, token_count_ms)
        self.last_token_count_ms = token_count_ms
        self.request_start_ms = None
        self.last_model_output_ms = None
        self.tool_output_after_model_output_ms = None
        self.usage_record = None
        if start_ms is None or end_ms is None:
            return None
        return estimated_latency_ms(start_ms, end_ms)


def _number_durable_event(event: UsageEvent, source_key: str,
                          ordinal: int) -> UsageEvent:
    """durable 事件入列时编号:``record:N`` 不与 UI 的整数序号碰撞。"""
    return replace(event, event_id=event_id_for(KIND, source_key,
                                                f"record:{ordinal}"))


def parse(item: UsageSource, stop_event=None, *, skip_token_count: int = 0, **_) -> ParseBatch | None:
    if item.context.get("cindy"):
        return parse_cindy(item, KIND)
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    events = []
    session_id = None
    project = None
    model = "codex"
    service_tier = None
    previous_cumulative = None
    # UI token_count 与 durable token_usage_record 是两条累计序列:中断过的会话
    # 两条线会分叉,重复判定各用各的基线,镜像配对则只看请求级用量。
    prev_cumulative_total = None
    prev_record_total = None
    pending_usage_mirror = None
    # durable 事件先挂起,等确认没有被它的 UI mirror 顶掉再入列。
    pending_usage_event = None
    pending_usage_source_key = None
    # 事件身份:UI 事件沿用旧实现(非合并按物理行号,合并按 UI 记录序号),
    # durable 事件用不会与整数序号碰撞的 record:N 命名空间。
    ui_token_seen = 0
    durable_seen = 0
    timer = RequestTimer()
    raw_token_seen = 0
    line_count = 0
    skip_token_count = max(0, int(skip_token_count or 0))
    try:
        segments = item.context.get("segments")
        merged = isinstance(segments, (list, tuple)) and len(segments) > 1
        for obj, segment_context, source_line in _records_for_item(item, stop_event):
            line_count = max(line_count, source_line)
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            # 计时观察跟随实际消费顺序(多段合并后的顺序)。
            timer.observe(obj)
            # durable `token_usage_record` 归一成 UI `token_count` 形状后,与 UI
            # 事件共用下面同一套累计、去重与配对规则。
            record = normalize_usage_record(obj)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            # 新一轮(session_meta/turn_context/task 边界)不可能是上一次请求的
            # 延迟 mirror,标记不得跨过这些边界;畸形的 durable 记录(归一失败,
            # 仍是 token_usage_record)同样清空,免得把下一条 legacy 请求误判成
            # mirror。
            if (obj.get("type") in {"session_meta", "turn_context", "token_usage_record"}
                    or (obj.get("type") == "event_msg"
                        and _is_task_started(payload))):
                pending_usage_mirror = None
            if record.get("type") == "session_meta":
                session_id = payload.get("id") or payload.get("session_id") or session_id
                project = payload.get("project") or payload.get("cwd") or payload.get("workdir") or project
                if not project and isinstance(payload.get("git"), dict):
                    project = payload["git"].get("repository_url")
            elif record.get("type") == "turn_context":
                if isinstance(payload.get("model"), str) and payload["model"].strip():
                    model = payload["model"].strip()
                if "service_tier" in payload:
                    service_tier = _service_tier(payload.get("service_tier"))
                project = payload.get("project") or payload.get("cwd") or payload.get("workdir") or project
            elif record.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                settings = payload.get("thread_settings")
                if isinstance(settings, dict):
                    if isinstance(settings.get("model"), str) and settings["model"].strip():
                        model = settings["model"].strip()
                    if "service_tier" in settings:
                        service_tier = _service_tier(settings.get("service_tier"))
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            raw_token_seen += 1
            is_usage_record = payload.get("usage_record") is True
            if not is_usage_record:
                # 只数非 usage_record 记录,durable 记录插进来也不会挪动已有 UI
                # 事件的序号,升级后同一批历史事件的 event_id 保持不变。
                ui_token_seen += 1
            info = payload.get("info")
            # durable 事件在下一条用量记录到来时才确认:这条若是它的 UI mirror,
            # 由 UI 事件(沿用旧 event_id)代表这次请求;否则 durable 事件以
            # record:N 入列。镜像可能在下一次读取才追加进来,标记不跨边界保留。
            mirror_duration_ms = None
            if pending_usage_event is not None:
                current_usage = (info.get("last_token_usage")
                                 if isinstance(info, dict) else None)
                if (not is_usage_record
                        and _same_request_usage(pending_usage_mirror, current_usage)):
                    # UI 事件代表这次请求:耗时沿用 durable 结算出的值(响应在
                    # 用量记录处结束),不再重复结算。
                    mirror_duration_ms = pending_usage_event.duration_ms
                    pending_usage_event = None
                else:
                    durable_seen += 1
                    events.append(_number_durable_event(
                        pending_usage_event, pending_usage_source_key,
                        durable_seen))
                    pending_usage_event = None
                pending_usage_source_key = None
            if not isinstance(info, dict):
                continue
            explicit_model = info.get("model") or payload.get("model")
            if isinstance(explicit_model, str):
                model_value = explicit_model.strip()
                if model_value:
                    model = model_value
            effective_model = (
                model if isinstance(explicit_model, str) and explicit_model.strip()
                else segment_context.get("model") or model
            )
            effective_tier = (
                _service_tier(segment_context.get("service_tier"))
                if "service_tier" in segment_context else service_tier
            )
            usage_last = info.get("last_token_usage")
            # durable 记录与 UI mirror 描述同一次请求,但两者的累计计数在中断后
            # 可能分叉,所以按本次请求的用量配对;标记可以跨追加保留(镜像可能
            # 在下一次读取才出现)。
            pending_usage_mirror = (
                usage_last if is_usage_record and isinstance(usage_last, dict)
                else None)
            cumulative = info.get("total_token_usage")
            cumulative_total = cumulative.get("total_tokens") if isinstance(cumulative, dict) else None
            if isinstance(cumulative_total, (int, float)) and not isinstance(cumulative_total, bool):
                baseline = prev_record_total if is_usage_record else prev_cumulative_total
                duplicate = cumulative_total > 0 and cumulative_total == baseline
                # 累计基线始终推进,包括重复快照、镜像与重放记录。
                if is_usage_record:
                    prev_record_total = cumulative_total
                else:
                    prev_cumulative_total = cumulative_total
            else:
                duplicate = False
            usage = usage_last
            if not isinstance(usage, dict) and isinstance(cumulative, dict):
                if isinstance(previous_cumulative, dict):
                    delta = {
                        key: int(cumulative.get(key) or 0) - int(previous_cumulative.get(key) or 0)
                        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
                    }
                    # Counters reset after compaction/new turns in some
                    # Codex builds.  A negative delta is not a negative
                    # usage event; treat the current cumulative snapshot
                    # as a fresh baseline instead.
                    usage = cumulative if any(value < 0 for value in delta.values()) else delta
                else:
                    usage = cumulative
            if isinstance(cumulative, dict):
                previous_cumulative = dict(cumulative)
            if not isinstance(usage, dict) or raw_token_seen <= skip_token_count:
                continue
            if duplicate:
                continue
            source_key = f"session:{session_id or _session_id_from_path(item.path)}"
            if is_usage_record:
                # 编号留到入列时定稿:被 UI mirror 顶掉的事件不占号。
                ordinal = f"record:{durable_seen + 1}"
            else:
                # 非合并用物理行号,合并用 UI 记录序号:与旧实现一致,升级不会
                # 改变已导入 UI 事件的 event_id。
                ordinal = ui_token_seen if merged else line_count
            event = make_event(
                kind=KIND,
                source_key=source_key,
                ordinal=ordinal,
                model=_decorate_model(effective_model, effective_tier,
                                       timestamp(obj.get("timestamp"))),
                requested_at=timestamp(obj.get("timestamp")),
                input_tokens=usage.get("input_tokens", 0),
                # Codex's output_tokens includes reasoning_output_tokens;
                # keep the IR's output bucket exclusive and let
                # UsageEvent fold reasoning back into completion_tokens.
                output_tokens=max(0, safe_int(usage.get("output_tokens", 0))
                                  - safe_int(usage.get("reasoning_output_tokens", 0))),
                cached_input_tokens=usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0)),
                reasoning_output_tokens=usage.get("reasoning_output_tokens", 0),
                total_tokens=usage.get("total_tokens"),
                project=project_name(project), session_id=str(session_id or _session_id_from_path(item.path)),
                input_includes_cache=True,
            )
            if event is not None:
                if mirror_duration_ms is not None:
                    event = replace(event, duration_ms=mirror_duration_ms)
                else:
                    # 每个真正产出的事件结算一次耗时:累计未变的重复快照与重放
                    # 跳过的记录都不算一次请求。
                    event = replace(event, duration_ms=timer.finish_request(
                        timestamp_ms(obj.get("timestamp")),
                        _cumulative_tokens(usage_last)) or 0)
            if event is None:
                continue
            if is_usage_record:
                pending_usage_event = event
                pending_usage_source_key = source_key
            else:
                events.append(event)
        # 文件末尾还没等到下一条用量记录:durable 事件按 record:N 入列。
        if pending_usage_event is not None:
            durable_seen += 1
            events.append(_number_durable_event(
                pending_usage_event, pending_usage_source_key, durable_seen))
            pending_usage_event = None
            pending_usage_source_key = None
    except (CodexMergeCancelled, OSError, EOFError, gzip.BadGzipFile, ValueError):
        return batch(
            [], line_count, skipped=True,
            warnings=("codex: 会话文件读取不完整，已保留上次同步状态。",),
        )
    return batch(events, line_count)


def replay_skips(sources: list[UsageSource], stop_event=None) -> dict[str, int]:
    # Cindy ledger databases are merged into this adapter's source list, but
    # Codex lineage analysis only understands rollout JSONL files.
    physical = [
        item.context.get("segments") or item.path
        for item in sources
        if not item.context.get("cindy")
        and not item.context.get("discovery_warnings")
    ]
    counts = replay_skip_counts(physical, stop_event)
    result = {}
    for item in sources:
        if item.context.get("cindy") or item.context.get("discovery_warnings"):
            continue
        segments = item.context.get("segments")
        paths = tuple(segments) if isinstance(segments, (list, tuple)) else (item.path,)
        # replay_skip_counts indexes a multi-segment source by its first
        # physical path, while the importer keys the cursor by the selected
        # representative. Translate that internal key back to the source
        # identity used by parse().
        skip = max((counts.get(str(path), 0) for path in paths), default=0)
        if skip:
            result[str(item.path)] = skip
    return result
