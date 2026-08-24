"""Offline Antigravity conversation SQLite/protobuf adapter."""

from pathlib import Path

from ..common import batch, config_value, make_event, project_name, source, sqlite_rows, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "antigravity"
LABEL = "Antigravity"
DEFAULT_PATH = Path.home() / ".gemini" / "antigravity"
ALWAYS_SCAN = True


def _fields(buf: bytes) -> dict[int, list[tuple[int, object]]]:
    out = {}
    pos = 0
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            break
        out.setdefault(number, []).append((wire, value))
    return out


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value, shift = 0, 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return value, pos


def _first(fields, number, wire=None):
    for candidate_wire, value in fields.get(number, []):
        if wire is None or candidate_wire == wire:
            return value
    return None


def _message(fields, number):
    value = _first(fields, number, 2)
    return _fields(value) if isinstance(value, bytes) else {}


def _string(fields, number):
    value = _first(fields, number, 2)
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else ""


def _metadata(blob: bytes):
    chat = _message(_fields(blob), 1)
    usage = _message(chat, 4)
    if not usage:
        return None
    values = {
        "input": _first(usage, 2) or 0, "output": _first(usage, 3) or 0,
        "cache": _first(usage, 5) or 0, "reasoning": _first(usage, 9) or 0,
        "response_id": _string(usage, 11),
        "display_name": _string(chat, 21),
        "response_model": _string(chat, 19),
    }
    if sum(values[key] for key in ("input", "output", "cache", "reasoning")) <= 0:
        return None
    start = _message(_message(chat, 9), 4)
    seconds = _first(start, 1) if start else None
    values["timestamp"] = timestamp(seconds)
    return values


def _step_timestamps(db: Path) -> dict[int, str]:
    result = {}
    rows = sqlite_rows(db, "SELECT idx,hex(metadata) AS blob FROM steps WHERE metadata IS NOT NULL ORDER BY idx")
    for row in rows:
        try:
            fields = _fields(bytes.fromhex(row["blob"] or ""))
            created = _message(fields, 1)
            seconds = _first(created, 1) if created else None
            value = timestamp(seconds)
            if value is not None:
                result[int(row["idx"])] = value
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
    return result


def _workspace(db: Path) -> str | None:
    rows = sqlite_rows(db, "SELECT hex(data) AS blob FROM trajectory_metadata_blob LIMIT 1")
    if not rows:
        return None
    try:
        fields = _fields(bytes.fromhex(rows[0]["blob"] or ""))
        workspace = _message(fields, 1)
        return _string(workspace, 1) if workspace else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    root = Path(config_value(software, "data_root", "path") or DEFAULT_PATH).expanduser()
    if root.is_file():
        return [source(root, conversations_dir=root.parent)]
    if root.name == "conversations":
        dirs = [root]
    else:
        dirs = [root / "conversations", root / "antigravity-cli" / "conversations"]
        # The CLI installation is a sibling of the standalone app directory,
        # not a child of it (e.g. ~/.gemini/antigravity-cli).
        if root.name == "antigravity":
            dirs.append(root.parent / "antigravity-cli" / "conversations")
    out = []
    seen = set()
    for directory in dirs:
        for path in walk_files(directory, (".db",)):
            if path.name == "db.sqlite":
                continue
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            out.append(source(path, conversations_dir=directory))
    return out


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    db = item.path
    rows = sqlite_rows(db, "SELECT idx,hex(data) AS blob FROM gen_metadata ORDER BY idx")
    step_times = _step_timestamps(db)
    workspace = _workspace(db)
    events = []
    for row in rows:
        try:
            record = _metadata(bytes.fromhex(row["blob"] or ""))
        except (ValueError, TypeError, IndexError, OverflowError):
            record = None
        if not record:
            continue
        requested_at = record.get("timestamp") or step_times.get(int(row["idx"]))
        event = make_event(
            kind=KIND, source_key="response" if record.get("response_id") else item.state_key,
            ordinal=record.get("response_id") or row["idx"],
            model=record.get("display_name") or record.get("response_model") or "unknown", requested_at=requested_at,
            input_tokens=record["input"], output_tokens=record["output"], cached_input_tokens=record["cache"],
            reasoning_output_tokens=record["reasoning"], project=project_name(workspace), session_id=db.stem,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
