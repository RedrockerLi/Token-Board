"""DeepSeek Harness session adapter.

DSH persists one session per directory as either a plain JSONL file or a
concatenated Zstandard log. The header carries the durable fork boundary, so
the adapter can remove inherited parent messages before emitting IR events.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "dsh"
LABEL = "DeepSeek Harness"
DEFAULT_PATH = Path.home() / ".dsh" / "sessions"
MAX_SESSION_FORMAT_VERSION = 3
SESSION_FILENAME = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl(\.zstd)?$")
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_DECOMPRESSED_SESSION_BYTES = 512 * 1024 * 1024


class _DshParseError(ValueError):
    """A selected DSH source is not safe to import."""


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


def _session_file_version(path: Path) -> int:
    match = SESSION_FILENAME.fullmatch(path.name)
    return int(match.group(1) or 0) if match else 0


def _session_candidates(root: Path) -> list[tuple[Path, int]]:
    if root.is_file():
        return [(root, _session_file_version(root))]
    selected_by_directory = {}
    for path in walk_files(root, (".jsonl", ".jsonl.zstd")):
        match = SESSION_FILENAME.fullmatch(path.name)
        if match is None:
            continue
        try:
            key = path.parent.resolve()
            stat = path.stat()
        except OSError:
            continue
        version = int(match.group(1) or 0)
        # DSH migrations leave several generations in one directory. The
        # highest canonical generation is authoritative; compressed wins only
        # when the generation is the same.
        rank = (version, 1 if match.group(2) else 0, stat.st_size,
                stat.st_mtime_ns)
        current = selected_by_directory.get(key)
        if current is None or rank > current[0]:
            selected_by_directory[key] = (rank, path, version)

    # A session can be copied between project buckets while it is archived.
    # Select the newest format first, then the largest/newest copy in that
    # generation. This prevents a stale V0/V1 copy from masking a V3 file.
    selected_by_session = {}
    for rank, path, version in selected_by_directory.values():
        session_key = _logical_session_key(path)
        current = selected_by_session.get(session_key)
        if current is None:
            selected_by_session[session_key] = (rank, path, version)
            continue
        current_rank, current_path, current_version = current
        try:
            candidate_stat = path.stat()
            current_stat = current_path.stat()
        except OSError:
            continue
        candidate = (version, candidate_stat.st_size, candidate_stat.st_mtime_ns,
                     rank[1])
        selected = (current_version, current_stat.st_size,
                    current_stat.st_mtime_ns, current_rank[1])
        if candidate > selected:
            selected_by_session[session_key] = (rank, path, version)
    return [
        (path, version)
        for _, path, version in sorted(
            selected_by_session.values(), key=lambda value: str(value[1]))
    ]


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
    for path, version in _session_candidates(root):
        if stop_event is not None and stop_event.is_set():
            break
        out.append(source(path, session_dir=path.parent,
                          file_version=version))
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
    if len(raw) > MAX_SESSION_FILE_BYTES:
        raise _DshParseError(
            f"session log too large ({len(raw)} bytes)")
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
    if len(result.stdout) > MAX_DECOMPRESSED_SESSION_BYTES:
        raise _DshParseError(
            f"decompressed session log too large ({len(result.stdout)} bytes)")
    return result.stdout.decode("utf-8", errors="replace")


def _seq(value):
    if type(value) is not int:
        return None
    return value if 0 <= value <= 9_007_199_254_740_991 else None


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


def _message_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _build_model(text: str, file_version: int) -> dict:
    header = None
    messages = []
    lines = text.splitlines()
    line_count = len(lines)
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
                message_id = _message_id(data.get("id"))
                messages.append({
                    "seq": _seq(record.get("seq")), "role": "user",
                    "time": requested_at, "usage": None, "model": "unknown",
                    "message_id": message_id,
                })
        elif record_type == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            messages.append({
                "seq": _seq(record.get("seq")), "role": "assistant",
                "time": requested_at, "usage": _usage(data.get("usage")),
                "model": _model_name(data),
                "message_id": _message_id(message.get("id")),
            })
    if (not isinstance(header, dict)
            or not isinstance(header.get("id"), str)
            or not header["id"]):
        raise _DshParseError("missing session header record")
    version = header.get("version")
    if type(version) is not int or version < 0 or version > MAX_SESSION_FORMAT_VERSION:
        raise _DshParseError(
            f"format version {version} is not supported (parser supports 0–"
            f"{MAX_SESSION_FORMAT_VERSION})")
    if version != file_version:
        raise _DshParseError(
            f"session header format version {version} disagrees with filename "
            f"version {file_version}")
    if version >= 2 and type(header.get("isSeeded")) is not bool:
        raise _DshParseError(
            f"format v{version} session header lacks isSeeded")
    inherited_seq = None
    # The marker scan above needs the header version, but the header may occur
    # after a malformed preamble. Re-scan only the tiny marker condition after
    # validating the header, preserving the order of the message pass.
    if version >= 2:
        for raw in lines:
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "session/end-seed":
                continue
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            if data.get("inherited") is True:
                seq = _seq(record.get("seq"))
                if seq is None:
                    raise _DshParseError(
                        "inherited end-seed marker lacks a valid seq")
                inherited_seq = seq
        if header.get("isSeeded") != (inherited_seq is not None):
            raise _DshParseError(
                "isSeeded disagrees with the inherited end-seed marker")
    has_user = any(message["role"] == "user" for message in messages)
    return {
        "session_id": header["id"],
        "parent_id": header.get("parentSession") if isinstance(header.get("parentSession"), str) else None,
        "format_version": version,
        "seed_length": (inherited_seq if version >= 2 else
                         (_seq(header.get("seedLength")) or 0)),
        "cwd": header.get("cwd"), "messages": messages, "has_user": has_user,
        "line_count": line_count,
    }


def _load_model(item: UsageSource, *, strict: bool = False) -> dict | None:
    try:
        text = _text(item.path)
        configured_version = item.context.get("file_version")
        file_version = (_session_file_version(item.path)
                        if configured_version is None else int(configured_version))
        return _build_model(text, file_version)
    except (OSError, ValueError, subprocess.SubprocessError):
        if strict:
            raise
        return None


def _replay_skip_count(child: dict, parent: dict | None) -> int:
    if parent is None or child["seed_length"] <= 0:
        return 0
    parent_index = 0
    previous = -1
    count = 0
    mixed_versions = child.get("format_version") != parent.get("format_version")
    for message in child["messages"]:
        seq = message["seq"]
        if seq is None or seq <= previous:
            return 0
        previous = seq
        if seq >= child["seed_length"]:
            break
        if mixed_versions:
            if not message.get("message_id"):
                return 0
            while (parent_index < len(parent["messages"])
                   and parent["messages"][parent_index].get("message_id")
                   != message["message_id"]):
                parent_index += 1
        else:
            while (parent_index < len(parent["messages"])
                   and parent["messages"][parent_index]["seq"] is not None
                   and parent["messages"][parent_index]["seq"] < seq):
                parent_index += 1
        source_message = (parent["messages"][parent_index]
                          if parent_index < len(parent["messages"]) else None)
        if (source_message is None
                or (not mixed_versions and source_message["seq"] != seq)
                or source_message["role"] != message["role"]):
            return 0
        if source_message["model"] != message["model"] or source_message["usage"] != message["usage"]:
            return 0
        parent_index += 1
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
    try:
        model = _load_model(item, strict=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return batch(
            [], 0, skipped=True,
            warnings=(f"dsh: skipping {item.path} ({exc})",),
        )
    if model is None:
        return batch([], 0, skipped=True,
                     warnings=(f"dsh: skipping {item.path} (invalid session)",))
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
            ordinal=(message.get("message_id") or message["seq"]
                     if message["seq"] is not None else index),
            model=message["model"], requested_at=message["time"],
            input_tokens=usage["input"], output_tokens=usage["output"],
            cached_input_tokens=usage["cache"],
            reasoning_output_tokens=usage["reasoning"],
            project=project, session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, model["line_count"])
