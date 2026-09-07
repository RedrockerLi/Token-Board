"""Offline Antigravity conversation SQLite/protobuf adapter."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from ..common import (
    batch,
    config_value,
    configured_extra_roots,
    make_event,
    project_name,
    safe_float,
    source,
    sqlite_rows_snapshot,
    timestamp,
    walk_files,
)
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


def _step_timestamps(db: Path, *, strict: bool = False) -> dict[int, str]:
    result = {}
    rows = sqlite_rows_snapshot(
        db,
        "SELECT idx,hex(metadata) AS blob FROM steps WHERE metadata IS NOT NULL ORDER BY idx",
        raise_on_error=strict,
    )
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
    rows = sqlite_rows_snapshot(
        db, "SELECT hex(data) AS blob FROM trajectory_metadata_blob LIMIT 1",
    )
    if not rows:
        return None
    try:
        fields = _fields(bytes.fromhex(rows[0]["blob"] or ""))
        workspace = _message(fields, 1)
        return _string(workspace, 1) if workspace else None
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _conversation_dirs(root: Path) -> list[Path]:
    if root.name == "conversations":
        return [root]
    if root.name in {"antigravity", "antigravity-cli", "antigravity-ide"}:
        return [
            root / "conversations",
            root.parent / "antigravity" / "conversations",
            root.parent / "antigravity-cli" / "conversations",
            root.parent / "antigravity-ide" / "conversations",
        ]
    if root.name == ".gemini":
        return [root / name / "conversations"
                for name in ("antigravity", "antigravity-cli", "antigravity-ide")]
    return [
        root / "conversations",
        root / "antigravity-cli" / "conversations",
        root / ".gemini" / "antigravity" / "conversations",
        root / ".gemini" / "antigravity-cli" / "conversations",
        root / ".gemini" / "antigravity-ide" / "conversations",
    ]


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = config_value(software, "data_root", "path")
    fixture_dirs = os.environ.get("VIBE_USAGE_ANTIGRAVITY_DIRS", "").strip()
    if configured:
        root = Path(configured).expanduser()
        if root.is_file():
            dirs = [(root.parent, True)]
        else:
            dirs = [(directory, True) for directory in _conversation_dirs(root)]
    elif fixture_dirs:
        # This hook names conversation directories themselves and is useful
        # for relocated stores and test fixtures.
        dirs = [
            (Path(value).expanduser(), False)
            for value in fixture_dirs.split(os.pathsep)
            if value.strip()
        ]
    else:
        dirs = [(directory, False) for directory in _conversation_dirs(DEFAULT_PATH)]
    for root in configured_extra_roots(software, KIND):
        dirs.extend((directory, True) for directory in _conversation_dirs(root))

    # The same directory can be reached through overlapping roots. Preserve a
    # strict flag when any route is explicitly configured so a broken DB cannot
    # be mistaken for an empty default store.
    directory_flags = {}
    for directory, strict in dirs:
        try:
            key = directory.resolve()
        except OSError:
            key = directory
        directory_flags[key] = (directory, directory_flags.get(key, (None, False))[1] or strict)

    sources = {}
    for directory, strict in directory_flags.values():
        for path in walk_files(directory, (".db", ".pb")):
            if path.name in {"db.sqlite", "db.pb"}:
                continue
            # A plain SQLite cascade is the authoritative reader for the same
            # conversation; do not fall back to the opaque legacy file when
            # both formats are present.
            if path.suffix == ".pb" and path.with_suffix(".db").is_file():
                continue
            try:
                key = path.resolve()
            except OSError:
                key = path
            item = source(
                path,
                conversations_dir=directory,
                strict=strict,
                legacy=path.suffix == ".pb",
            )
            previous = sources.get(key)
            if previous is None or (strict and not previous.context.get("strict")):
                sources[key] = item
    return list(sources.values())


def _parse_windows_process_list(text: str) -> list[dict[str, str]]:
    servers = []
    pid = ""
    command_line = ""

    def finish() -> None:
        if not pid or not command_line:
            return
        if re.search(r"(?:WMIC|powershell|pwsh)\.exe", command_line, re.I):
            return
        match = re.search(r"--csrf_token\s+([0-9a-f-]+)", command_line, re.I)
        if match:
            servers.append({"pid": pid, "csrf_token": match.group(1)})

    for raw in text.splitlines():
        line = raw.strip()
        is_pid = line.startswith("ProcessId=")
        is_command = line.startswith("CommandLine=")
        if line == "---" or (is_pid and pid) or (is_command and command_line):
            finish()
            pid = ""
            command_line = ""
        if is_pid:
            pid = line.split("=", 1)[1].strip()
        elif is_command:
            command_line = line.split("=", 1)[1].strip()
    finish()
    return servers


def _language_servers() -> list[dict[str, str]]:
    """Find running Antigravity language servers that expose legacy history."""
    if sys.platform == "win32":
        powershell_script = (
            'Get-CimInstance Win32_Process -Filter '
            '"CommandLine LIKE \'%antigravity%language_server%\'" | '
            'ForEach-Object { "---"; "ProcessId=" + $_.ProcessId; '
            '"CommandLine=" + $_.CommandLine }'
        )
        commands = [
            [executable, "-NoProfile", "-NonInteractive", "-Command", powershell_script]
            for executable in ("powershell.exe", "pwsh.exe")
        ]
        commands.append([
            "wmic", "process", "where", "CommandLine like '%antigravity%language_server%'",
            "get", "ProcessId,CommandLine", "/format:list",
        ])
        for command in commands:
            try:
                output = subprocess.check_output(
                    command, text=True, stderr=subprocess.DEVNULL, timeout=4,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if not output.strip():
                continue
            return _parse_windows_process_list(output)
        return []
    try:
        output = subprocess.check_output(
            ["ps", "aux"], text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    servers = []
    for line in output.splitlines():
        if "language_server" not in line.lower() or "antigravity" not in line.lower():
            continue
        if "grep" in line.lower():
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        match = re.search(r"--csrf_token\s+([0-9a-f-]+)", line, re.I)
        if match:
            servers.append({"pid": parts[1], "csrf_token": match.group(1)})
    return servers


def _listening_ports(pid: str) -> list[int]:
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL, timeout=5,
            )
            ports = []
            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[-1] != str(pid) or "LISTENING" not in parts:
                    continue
                match = re.search(r":(\d+)$", parts[1])
                if match:
                    ports.append(int(match.group(1)))
            return ports
        output = subprocess.check_output(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-nP", "-a", "-p", str(pid)],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(match.group(1)) for match in re.finditer(
        r":(\d+)\s+\(LISTEN\)", output,
    )]


def _rpc_post(port: int, method: str, body: dict, csrf_token: str,
              timeout: float = 10.0) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": csrf_token,
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise RuntimeError(f"Antigravity RPC returned HTTP {status}")
        payload = response.read()
    value = json.loads(payload.decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _legacy_trajectory(cascade_id: str) -> dict | None:
    """Ask each running language server for a legacy `.pb` cascade."""
    for server in _language_servers():
        ports = _listening_ports(server.get("pid", ""))
        for port in ports:
            token = str(server.get("csrf_token") or "")
            try:
                _rpc_post(port, "GetWorkspaceInfos", {}, token, timeout=3.0)
                response = _rpc_post(
                    port, "GetCascadeTrajectory", {"cascadeId": cascade_id},
                    token,
                )
            except Exception:
                continue
            trajectory = response.get("trajectory") if isinstance(response, dict) else None
            if isinstance(trajectory, dict):
                return trajectory
    return None


_MODEL_NORMALIZE_MAP = {
    "claude-opus-4-6-thinking": "claude-opus-4-6",
    "claude-sonnet-4-6-thinking": "claude-sonnet-4-6",
    "gemini-3.1-pro-high": "gemini-3.1-pro",
    "gemini-3.1-pro-low": "gemini-3.1-pro",
    "gemini-3-pro-high": "gemini-3-pro",
    "gemini-3-pro-low": "gemini-3-pro",
}
_PLACEHOLDER_MODEL_MAP = {
    "MODEL_PLACEHOLDER_M37": "gemini-3.1-pro",
    "MODEL_PLACEHOLDER_M36": "gemini-3.1-pro",
    "MODEL_PLACEHOLDER_M47": "gemini-3-flash",
    "MODEL_PLACEHOLDER_M35": "claude-sonnet-4-6",
    "MODEL_PLACEHOLDER_M26": "claude-opus-4-6",
    "MODEL_OPENAI_GPT_OSS_120B_MEDIUM": "gpt-oss-120b",
}


def _legacy_model(chat_model: dict) -> str:
    display = chat_model.get("modelDisplayName")
    if display:
        return str(display)
    response_model = str(chat_model.get("responseModel") or "")
    if response_model:
        return _MODEL_NORMALIZE_MAP.get(response_model, response_model)
    return _PLACEHOLDER_MODEL_MAP.get(str(chat_model.get("model") or ""), "unknown")


def _parse_legacy(item: UsageSource, stop_event=None) -> ParseBatch:
    trajectory = _legacy_trajectory(item.path.stem)
    if trajectory is None:
        return batch(
            [], 0, skipped=True,
            warnings=(
                "antigravity: 旧格式会话暂时无法读取，请打开对应的 Antigravity IDE/App 后重试。",
            ),
        )
    metadata_root = trajectory.get("metadata")
    workspaces = metadata_root.get("workspaces", []) if isinstance(metadata_root, dict) else []
    project = "unknown"
    if isinstance(workspaces, list) and workspaces:
        workspace = workspaces[0] if isinstance(workspaces[0], dict) else {}
        repository = workspace.get("repository")
        if isinstance(repository, dict) and repository.get("computedName"):
            project = str(repository["computedName"])
        else:
            project = project_name(workspace.get("workspaceFolderAbsoluteUri"))

    events = []
    metadata = trajectory.get("generatorMetadata")
    if not isinstance(metadata, list):
        metadata = []
    for entry in metadata:
        if stop_event is not None and stop_event.is_set():
            return batch(events, len(metadata), skipped=True)
        chat_model = entry.get("chatModel") if isinstance(entry, dict) else None
        if not isinstance(chat_model, dict):
            continue
        started = chat_model.get("chatStartMetadata")
        started = started.get("createdAt") if isinstance(started, dict) else None
        requested_at = timestamp(started)
        if requested_at is None:
            continue
        retries = chat_model.get("retryInfos")
        if not isinstance(retries, list):
            retries = []
        for retry in retries:
            if not isinstance(retry, dict) or not isinstance(retry.get("usage"), dict):
                continue
            usage = retry["usage"]
            response_id = str(usage.get("responseId") or "").strip()
            event = make_event(
                kind=KIND,
                source_key="response" if response_id else item.state_key,
                ordinal=response_id or len(events),
                model=_legacy_model(chat_model),
                requested_at=requested_at,
                input_tokens=safe_float(usage.get("inputTokens")),
                output_tokens=safe_float(usage.get("outputTokens")),
                cached_input_tokens=safe_float(usage.get("cacheReadTokens")),
                reasoning_output_tokens=safe_float(usage.get("thinkingOutputTokens")),
                total_tokens=(
                    safe_float(usage.get("inputTokens"))
                    + safe_float(usage.get("outputTokens"))
                    + safe_float(usage.get("thinkingOutputTokens"))
                ),
                project=project,
                session_id=item.path.stem,
            )
            if event:
                events.append(event)
    return batch(events, len(metadata))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    if item.context.get("legacy"):
        return _parse_legacy(item, stop_event)
    db = item.path
    if not db.is_file():
        return batch([], 0)
    strict = bool(item.context.get("strict"))
    try:
        rows = sqlite_rows_snapshot(
            db, "SELECT idx,hex(data) AS blob FROM gen_metadata ORDER BY idx",
            raise_on_error=True,
        )
        step_times = _step_timestamps(db, strict=strict)
    except (OSError, sqlite3.Error):
        return batch(
            [], 0, skipped=True,
            warnings=(
                "antigravity: 会话数据库读取失败，已保留上次同步状态。",
            ),
        )
    workspace = _workspace(db)
    events = []
    for row in rows:
        if stop_event is not None and stop_event.is_set():
            return batch(events, len(rows), skipped=True)
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
            reasoning_output_tokens=record["reasoning"],
            total_tokens=record["input"] + record["output"] + record["reasoning"],
            project=project_name(workspace), session_id=db.stem,
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
