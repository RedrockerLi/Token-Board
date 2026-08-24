"""DeepSeek Harness session adapter.

DSH persists one session per directory as either a plain JSONL file or a
concatenated Zstandard log. The header carries the durable fork boundary, so
the adapter can remove inherited parent messages before emitting IR events.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "dsh"
LABEL = "DeepSeek Harness"
DEFAULT_PATH = Path.home() / ".dsh" / "sessions"
SESSION_FORMAT_VERSION = 0


def _sessions_root(software: dict) -> Path:
    configured = config_value(software, "data_root", "path")
    if not configured and os.environ.get("VIBE_USAGE_DSH_SESSIONS", "").strip():
        # This is a fixture/relocation hook for the sessions directory itself,
        # unlike DSH_HOME which names the parent home.
        return Path(os.environ["VIBE_USAGE_DSH_SESSIONS"]).expanduser()
    value = Path(configured).expanduser() if configured else Path(
        os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser()
    if value.is_file() or value.name in {"session.jsonl", "session.jsonl.zstd"}:
        return value
    return value if value.name == "sessions" else value / "sessions"


def _session_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    selected_by_directory = {}
    for path in walk_files(root, ("session.jsonl", "session.jsonl.zstd")):
        try:
            key = path.parent.resolve()
            stat = path.stat()
        except OSError:
            continue
        # Prefer the compressed file when both representations exist. It is
        # the authoritative current log in DSH's migration window.
        rank = (1 if path.name.endswith(".zstd") else 0, stat.st_size, stat.st_mtime_ns)
        current = selected_by_directory.get(key)
        if current is None or rank > current[0]:
            selected_by_directory[key] = (rank, path)

    # A session can be copied between project buckets while it is archived.
    # The on-disk directory name is the stable id in DSH's layout; keep the
    # largest/newest copy so its newer usage is not hidden by traversal order.
    selected_by_session = {}
    for rank, path in selected_by_directory.values():
        session_key = _logical_session_key(path)
        current = selected_by_session.get(session_key)
        if current is None or (path.stat().st_size, path.stat().st_mtime_ns,
                               rank[0]) > (current[1].stat().st_size,
                                           current[1].stat().st_mtime_ns,
                                           current[0][0]):
            selected_by_session[session_key] = (rank, path)
    return [path for _, path in sorted(selected_by_session.values(),
                                       key=lambda value: str(value[1]))]


def _logical_session_key(path: Path) -> str:
    """Read the header id when possible; fall back to the directory name."""
    try:
        text = _text(path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return path.parent.name
    for raw in text.splitlines():
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (isinstance(record, dict) and record.get("type") == "session"
                and isinstance(record.get("id"), str) and record["id"]):
            return record["id"]
    return path.parent.name


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = _sessions_root(software)
    out = []
    for path in _session_files(root):
        if stop_event is not None and stop_event.is_set():
            break
        out.append(source(path, session_dir=path.parent))
    return out


_ZSTD_MAGIC = 0xFD2FB528
_SKIPPABLE_MAGIC_MIN = 0x184D2A50
_SKIPPABLE_MAGIC_MAX = 0x184D2A5F


def _split_zstd_frames(buffer: bytes) -> list[tuple[int, int]]:
    """Return complete standard-frame ranges, ignoring an incomplete tail."""
    frames = []
    position = 0
    length = len(buffer)
    while position < length:
        if position + 4 > length:
            break
        magic = struct.unpack_from("<I", buffer, position)[0]
        if _SKIPPABLE_MAGIC_MIN <= magic <= _SKIPPABLE_MAGIC_MAX:
            if position + 8 > length:
                break
            size = struct.unpack_from("<I", buffer, position + 4)[0]
            end = position + 8 + size
            if end > length:
                break
            position = end
            continue
        if magic != _ZSTD_MAGIC:
            raise ValueError(f"invalid Zstandard frame magic at byte {position}")

        start = position
        position += 4
        if position >= length:
            break
        descriptor = buffer[position]
        position += 1
        if descriptor & 0x18:
            raise ValueError(f"reserved Zstandard frame-header bit at byte {position - 1}")
        single_segment = bool(descriptor & 0x20)
        has_checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        content_size_flag = descriptor >> 6
        dictionary_bytes = {0: 0, 1: 1, 2: 2, 3: 4}[dictionary_flag]
        content_size_bytes = (
            (1 if single_segment else 0) if content_size_flag == 0
            else 1 << content_size_flag
        )
        remaining = ((0 if single_segment else 1) + dictionary_bytes
                     + content_size_bytes)
        if position + remaining > length:
            break
        position += remaining

        while True:
            if position + 3 > length:
                return frames
            block_header = int.from_bytes(buffer[position:position + 3], "little")
            position += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            if block_type == 0x03:
                raise ValueError(f"reserved Zstandard block type at byte {position - 3}")
            payload_bytes = 1 if block_type == 0x01 else block_size
            if position + payload_bytes > length:
                return frames
            position += payload_bytes
            if last_block:
                break
        if has_checksum:
            if position + 4 > length:
                return frames
            position += 4
        frames.append((start, position))
    return frames


def _text(path: Path) -> str:
    raw = path.read_bytes()
    if not path.name.endswith(".zstd"):
        return raw.decode("utf-8", errors="replace")
    zstd = shutil.which("zstd")
    if not zstd:
        raise OSError("zstd CLI is required to read DeepSeek Harness logs")
    frames = _split_zstd_frames(raw)
    if not frames:
        raise ValueError("no complete zstd frames found")
    # DSH appends frames and may leave a torn final frame while the session is
    # still live. Decode all complete frames only; zstd CLI otherwise rejects
    # the entire concatenated stream and silently loses the earlier usage.
    complete = b"".join(raw[start:end] for start, end in frames)
    result = subprocess.run(
        [zstd, "-d", "-c"], input=complete, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=True, timeout=30,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _seq(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None

    def number(name):
        try:
            return max(0, float(value.get(name, 0) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    input_tokens = number("inputTokens") + number("cacheWriteTokens")
    cached = number("cacheReadTokens")
    total_output = number("outputTokens")
    reasoning = min(total_output, number("reasoningTokens"))
    output = max(0, total_output - reasoning)
    if input_tokens + cached + output + reasoning <= 0:
        return None
    return {"input": input_tokens, "cache": cached, "output": output,
            "reasoning": reasoning}


def _model_name(data: dict) -> str:
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    source_data = message.get("source") if isinstance(message.get("source"), dict) else {}
    value = source_data.get("model")
    return str(value).strip() if isinstance(value, str) and value.strip() else "unknown"


def _load_model(item: UsageSource) -> dict | None:
    try:
        text = _text(item.path)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    header = None
    messages = []
    line_count = 0
    for line_no, raw in enumerate(text.splitlines(), 1):
        line_count = line_no
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        if header is None and record.get("type") == "session":
            header = record
        requested_at = timestamp(record.get("time"))
        if requested_at is None:
            continue
        record_type = record.get("type")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if record_type == "user/message":
            source_data = data.get("source") if isinstance(data.get("source"), dict) else {}
            if source_data.get("kind") == "user":
                messages.append({
                    "seq": _seq(record.get("seq")), "role": "user",
                    "time": requested_at, "usage": None, "model": "unknown",
                })
        elif record_type == "assistant/message":
            messages.append({
                "seq": _seq(record.get("seq")), "role": "assistant",
                "time": requested_at, "usage": _usage(data.get("usage")),
                "model": _model_name(data),
            })
    if (not isinstance(header, dict)
            or not isinstance(header.get("id"), str)
            or not header["id"]):
        return None
    version = header.get("version")
    if version != SESSION_FORMAT_VERSION:
        return None
    has_user = any(message["role"] == "user" for message in messages)
    return {
        "session_id": header["id"],
        "parent_id": header.get("parentSession") if isinstance(header.get("parentSession"), str) else None,
        "seed_length": _seq(header.get("seedLength")) or 0,
        "cwd": header.get("cwd"), "messages": messages, "has_user": has_user,
        "line_count": line_count,
    }


def _replay_skip_count(child: dict, parent: dict | None) -> int:
    if parent is None or child["seed_length"] <= 0:
        return 0
    parent_by_seq = {
        message["seq"]: message for message in parent["messages"]
        if message["seq"] is not None
    }
    previous = -1
    count = 0
    for message in child["messages"]:
        seq = message["seq"]
        if seq is None or seq <= previous:
            return 0
        previous = seq
        if seq >= child["seed_length"]:
            break
        source_message = parent_by_seq.get(seq)
        if source_message is None or source_message["role"] != message["role"]:
            return 0
        if source_message["model"] != message["model"] or source_message["usage"] != message["usage"]:
            return 0
        count += 1
    return count


def replay_skips(sources: list[UsageSource], stop_event=None) -> dict[str, int]:
    models = {}
    for item in sources:
        if stop_event is not None and stop_event.is_set():
            return {}
        model = _load_model(item)
        if model is not None:
            models[str(item.path)] = model
    by_session = {model["session_id"]: model for model in models.values()}
    result = {}
    for item in sources:
        model = models.get(str(item.path))
        if model is None or not model.get("parent_id"):
            continue
        count = _replay_skip_count(model, by_session.get(model["parent_id"]))
        if count:
            result[str(item.path)] = count
    return result


def parse(item: UsageSource, stop_event=None, *, skip_token_count: int = 0, **_) -> ParseBatch:
    model = _load_model(item)
    if model is None:
        return batch([], 0)
    # Plugin-driven assistant-only logs are not user agent sessions. Keep
    # their usage out of the local import just as the reference parser does.
    if not model.get("has_user"):
        return batch([], model["line_count"])
    try:
        skip = max(0, int(skip_token_count or 0))
    except (TypeError, ValueError):
        skip = 0
    project = project_name(model.get("cwd") or item.path.parent.parent.name)
    session_id = model["session_id"]
    events = []
    for index, message in enumerate(model["messages"]):
        if stop_event is not None and stop_event.is_set():
            break
        if index < skip or message.get("usage") is None:
            continue
        usage = message["usage"]
        event = make_event(
            kind=KIND, source_key=f"session:{session_id}",
            ordinal=message["seq"] if message["seq"] is not None else index,
            model=message["model"], requested_at=message["time"],
            input_tokens=usage["input"], output_tokens=usage["output"],
            cached_input_tokens=usage["cache"],
            reasoning_output_tokens=usage["reasoning"],
            project=project, session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, model["line_count"])
