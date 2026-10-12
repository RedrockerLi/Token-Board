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

# steps.metadata 字段 3 的步骤来源枚举(与 RPC 的 CORTEX_STEP_SOURCE_* 对应):
# 4 = 用户轮次,2 = 模型轮次,其余(系统/工具/未指定)只贡献时间戳。
STEP_SOURCE_USER = 4
STEP_SOURCE_MODEL = 2


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


def _parse_usage_message(fields) -> dict:
    """解码一条 ModelUsage 消息。

    gen_metadata 的 chatModel.usage(1.4)、其重试条目(1.17.2)、steps.metadata
    的 usage(9)与重试(28.2)是同一个线上格式,共用一个解码器:

      modelId = 1, input = 2, totalOutput = 3(含 thinking), cacheCreation = 4,
      cacheRead = 5, messageId = 7, thinking = 9, visibleOutput = 10,
      responseId = 11, providerMessageId = 12

    字段 3 是输出总量(可见部分 + thinking)。宿主会把 reasoning 折回
    completion,所以 output 只保留可见部分,thinking 单独留给 reasoning;
    把 thinking 留在 output 里的每个 token 都会被计两次。
    """
    input_tokens = _first(fields, 2, 0) or 0
    total_output = _first(fields, 3, 0) or 0
    cache_creation = _first(fields, 4, 0) or 0
    cache_read = _first(fields, 5, 0) or 0
    thinking = _first(fields, 9, 0) or 0
    visible_output = _first(fields, 10, 0)
    if visible_output is None:
        output_tokens = max(0, total_output - thinking)
    else:
        output_tokens = min(visible_output, total_output)
    return {
        "model_id": _first(fields, 1, 0) or 0,
        "input": input_tokens,
        "output": output_tokens,
        "cache_creation": cache_creation,
        "cache_read": cache_read,
        # 缺少 visible 字段时,可见部分之外的余量就是 thinking;
        # 两者始终加回字段 3 的总量。
        "thinking": max(thinking, total_output - output_tokens),
        "message_id": _string(fields, 7),
        "response_id": _string(fields, 11),
        "provider_message_id": _string(fields, 12),
        "total_output": total_output,
    }


def _has_token_usage(usage) -> bool:
    """解码后的 usage 是否带有任何真实 token 计数。"""
    if not usage:
        return False
    return bool(usage.get("input") or usage.get("total_output")
                or usage.get("cache_creation") or usage.get("cache_read")
                or usage.get("thinking"))


def _parse_retry_usages(fields, number: int) -> list[dict]:
    """解码 number 字段下每个重试条目(条目字段 2 = usage)。"""
    out = []
    for wire, value in fields.get(number, []):
        if wire != 2 or not isinstance(value, bytes):
            continue
        usage = _first(_fields(value), 2, 2)
        if isinstance(usage, bytes):
            out.append(_parse_usage_message(_fields(usage)))
    return out


def _parse_gen_metadata_blob(blob: bytes) -> dict | None:
    """解析一行 gen_metadata;没有 token 用量(错误/规划占位)返回 None。"""
    chat = _message(_fields(blob), 1)
    usage = _first(chat, 4, 2)
    if not isinstance(usage, bytes):
        return None
    parsed = _parse_usage_message(_fields(usage))
    if not _has_token_usage(parsed):
        return None
    seconds = _first(_message(_message(chat, 9), 4), 1, 0)
    return {
        **parsed,
        "timestamp": timestamp(seconds) if seconds else None,
        "display_name": _string(chat, 21),
        "response_model": _string(chat, 19),
        "retry_usages": _parse_retry_usages(chat, 17),
    }


def _decode_step_usage(fields) -> dict:
    """解码 steps.metadata 自带的用量(字段 9)、重试(字段 28)与模型信息(24)。

    用量可以只写在 step 上(字段 9),generator 行可以完全没有用量。
    """
    usage_bytes = _first(fields, 9, 2)
    model_info = _message(fields, 24)
    return {
        "usage": (_parse_usage_message(_fields(usage_bytes))
                  if isinstance(usage_bytes, bytes) else None),
        "retry_usages": _parse_retry_usages(fields, 28),
        "model_name": ((_string(model_info, 12) or _string(model_info, 8))
                       if model_info else ""),
        "model_id": ((_first(model_info, 1, 0) or 0) if model_info else 0),
    }


def _step_timestamp(fields) -> str | None:
    """取 step 自己的时钟:字段 1.1 优先,缺失时回退字段 8.1。

    越界或损坏的 varint 只会让这一行没有时钟,不能让整轮导入失败。
    """
    seconds = None
    for number in (1, 8):
        message = _message(fields, number)
        seconds = _first(message, 1, 0) if message else None
        if seconds:
            break
    if not seconds:
        return None
    try:
        return timestamp(seconds)
    except (TypeError, ValueError, OverflowError):
        return None


def _row_index(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_usage_records(db: Path) -> list[dict]:
    """读取一个级联 .db 的全部 gen_metadata 用量记录(带表内 idx)。"""
    rows = sqlite_rows_snapshot(
        db,
        "SELECT idx,hex(data) AS blob FROM gen_metadata ORDER BY idx",
        raise_on_error=True,
    )
    records = []
    for row in rows:
        blob = row["blob"]
        if not blob:
            continue
        try:
            record = _parse_gen_metadata_blob(bytes.fromhex(blob))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue  # 单个损坏 blob 不能拖垮其余行
        if record:
            record["idx"] = _row_index(row["idx"])
            records.append(record)
    return records


def _read_step_rows(db: Path, *, strict: bool = False) -> dict:
    """单遍扫描 steps 表,同时取回三件事:

      - usages:step 自带的用量(字段 9 + 字段 28 的重试条目);用量可以
        只落在 step 上,generator 行没有用量的库也能读出 token;
      - timestamps:idx → 时钟,含不产生事件的系统/工具步骤,用于补齐
        没有内嵌 chatStartMetadata 的 gen_metadata 行;
      - events:用户/模型轮次,按 idx 排序,供会话计时使用。

    步骤来源见 STEP_SOURCE_* 常量;其余步骤只贡献时间戳。
    """
    rows = sqlite_rows_snapshot(
        db,
        "SELECT idx,hex(metadata) AS blob FROM steps "
        "WHERE metadata IS NOT NULL ORDER BY idx",
        raise_on_error=strict,
    )
    usages: list[dict] = []
    timestamps: dict[int, str] = {}
    events: list[dict] = []
    for row in rows:
        blob = row["blob"]
        if not blob:
            continue
        try:
            fields = _fields(bytes.fromhex(blob))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue  # 单个损坏 blob 不能拖垮其余行
        idx = _row_index(row["idx"])
        value = _step_timestamp(fields)
        if value is not None and idx is not None:
            timestamps[idx] = value
        source = _first(fields, 3, 0)
        role = ("user" if source == STEP_SOURCE_USER
                else "assistant" if source == STEP_SOURCE_MODEL else None)
        if role and value is not None:
            events.append({"role": role, "timestamp": value})
        step_usage = _decode_step_usage(fields)
        if step_usage["usage"] is None and not step_usage["retry_usages"]:
            continue
        usages.append({
            "idx": idx,
            "timestamp": value,
            "model_name": step_usage["model_name"],
            "model_id": step_usage["model_id"],
            "usage": step_usage["usage"],
            "retry_usages": step_usage["retry_usages"],
        })
    return {"usages": usages, "timestamps": timestamps, "events": events}


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
    extra_roots = configured_extra_roots(software, KIND)
    discovery_warnings = []
    for root in extra_roots:
        extra_dirs = _conversation_dirs(root)
        if not any(directory.is_dir() for directory in extra_dirs):
            discovery_warnings.append(
                f"antigravity: 额外数据根目录不可用，已保留上次同步状态: {root}"
            )
        dirs.extend((directory, True) for directory in extra_dirs)

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
    if discovery_warnings:
        warning_path = extra_roots[0] if extra_roots else DEFAULT_PATH
        return [source(
            warning_path,
            key="antigravity-discovery",
            discovery_warnings=tuple(dict.fromkeys(discovery_warnings)),
        )]
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

# 模型数字 id(gen_metadata 的 chatModel 字段 3 / ModelUsage 字段 1)到名称,
# 对应当前构建实际提供的模型。表外的 id 回退到本条级联最近一个具名模型,
# 而不是编造一个无法定价的占位名。
_MODEL_ID_NAMES = {
    246: "gemini-2.5-pro",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    330: "gemini-2.5-flash-lite",
    281: "claude-4-sonnet",
    282: "claude-4-sonnet",
    290: "claude-4-opus",
    291: "claude-4-opus",
    333: "claude-4.5-sonnet",
    334: "claude-4.5-sonnet",
    340: "claude-4.5-haiku",
    341: "claude-4.5-haiku",
    342: "gpt-oss-120b-medium",
    1071: "gemini-3.6-flash-high",
    1072: "gemini-3.6-flash-medium",
    1073: "gemini-3.6-flash-low",
    1298: "gemini-3.7-flash-high",
    1299: "gemini-3.7-flash-medium",
    1300: "gemini-3.7-flash-low",
    1318: "gemini-3.8-flash-high",
    1319: "gemini-3.8-flash-medium",
    1320: "gemini-3.8-flash-low",
}


def _normalize_model(raw: str) -> str:
    return _MODEL_NORMALIZE_MAP.get(str(raw), str(raw))


def _legacy_model(chat_model: dict) -> str:
    display = chat_model.get("modelDisplayName")
    if display:
        return str(display)
    response_model = str(chat_model.get("responseModel") or "")
    if response_model:
        return _normalize_model(response_model)
    return _PLACEHOLDER_MODEL_MAP.get(str(chat_model.get("model") or ""), "unknown")


def _model_from_record(record: dict) -> str:
    """.db 用量记录的模型回退链:

    displayName → responseModel → step modelName → 数字 id 表 → unknown。
    displayName 是权威的人类可读名(带推理档位),原样使用;它缺失时
    回退到 slug 与数字 id。
    """
    if record.get("display_name"):
        return record["display_name"]
    if record.get("response_model"):
        return _normalize_model(record["response_model"])
    if record.get("model_name"):
        return _normalize_model(record["model_name"])
    model_id = record.get("model_id") or 0
    if model_id and model_id in _MODEL_ID_NAMES:
        return _MODEL_ID_NAMES[model_id]
    return "unknown"


def _usage_identity_keys(record: dict) -> list[str]:
    """命名同一次 provider 调用的身份键;共享任意一个键即视为同一条记录。"""
    keys = []
    if record.get("response_id"):
        keys.append(f"response:{record['response_id']}")
    if record.get("provider_message_id"):
        keys.append(f"provider:{record['provider_message_id']}")
    if record.get("message_id"):
        keys.append(f"message:{record['message_id']}")
    return keys


def _usage_score(record: dict) -> int:
    """保留负载更丰富副本时的排序分数。"""
    return ((record.get("input") or 0) + (record.get("total_output") or 0)
            + (record.get("cache_read") or 0) + (record.get("cache_creation") or 0))


def _richer_usage_record(a: dict, b: dict) -> dict:
    """保留负载更丰富的一份,借用被丢弃副本的时钟与模型名。"""
    rich, poor = (b, a) if _usage_score(b) > _usage_score(a) else (a, b)
    return {
        **rich,
        "timestamp": rich.get("timestamp") or poor.get("timestamp"),
        "display_name": rich.get("display_name") or poor.get("display_name") or "",
        "response_model": rich.get("response_model") or poor.get("response_model") or "",
    }


def _stable_identity(keys) -> str | None:
    """从一组身份键里选一条与保留哪份副本无关的稳定代表。

    优先级 response > provider > message:同一次调用无论由哪张表的副本
    胜出,event_id 都不变,增量导入的去重凭据才不会翻倍。
    """
    for prefix in ("response:", "provider:", "message:"):
        candidates = sorted(key for key in keys if key.startswith(prefix))
        if candidates:
            return candidates[0]
    return None


def _merge_cascade_usage(gen_records: list[dict], step_usages: list[dict]) -> list[dict]:
    """合并一条级联里两张表各自可能携带的用量记录。

    gen_metadata 与 steps 都可能是同一次调用的载体,两种载体都用 provider
    的 response id(部分构建只有 message id)标识调用,因此共享任意身份键
    的两条记录合并、取负载更丰富的一份;两张都计数会让每个轮次重复计费。
    重试条目按同一身份键并入其所属的调用,另一次真实尝试保留自己的 id
    单独计数。

    完全没有身份的 step 记录,只有在整条级联没有任何 generator 用量时
    才保留;旁边有 generator 用量时丢弃,避免同一轮次计两次。
    """
    groups: list[dict] = []
    by_identity: dict[str, dict] = {}

    def merge(record: dict) -> None:
        keys = _usage_identity_keys(record)
        hits: dict[int, dict] = {}
        for key in keys:
            group = by_identity.get(key)
            if group is not None:
                hits.setdefault(id(group), group)
        if not hits:
            # source_idx 只服务于 event 序号的稳定性,不参与任何额度口径。
            target = {"record": record, "keys": set(),
                      "source_idx": record.get("source_idx", record.get("idx"))}
            groups.append(target)
        else:
            target = next(iter(hits.values()))
            for other in hits.values():
                if other is target:
                    continue
                target["record"] = _richer_usage_record(target["record"], other["record"])
                for key in other["keys"]:
                    by_identity[key] = target
                    target["keys"].add(key)
                # 按对象身份移除,避免字典值相等时删错分组。
                for position, candidate in enumerate(groups):
                    if candidate is other:
                        del groups[position]
                        break
            target["record"] = _richer_usage_record(target["record"], record)
        for key in keys:
            by_identity[key] = target
            target["keys"].add(key)

    for record in gen_records:
        merge(record)
        for retry in record.get("retry_usages") or ():
            merge({**retry, "timestamp": record.get("timestamp"),
                   "source_idx": record.get("idx")})
    step_usage_stands_alone = not gen_records
    for row in step_usages:
        for usage in (row.get("usage"), *(row.get("retry_usages") or ())):
            if not _has_token_usage(usage):
                continue
            if not step_usage_stands_alone and not _usage_identity_keys(usage):
                continue
            merge({
                **usage,
                "timestamp": row.get("timestamp"),
                "display_name": "",
                "response_model": "",
                "model_name": row.get("model_name"),
                "source_idx": row.get("idx"),
            })

    # 记录按首次出现顺序输出;解析模型,并把本条级联最近一个具名模型延续
    # 给只带数字 id 的记录。
    carried_model = ""
    merged = []
    for group in groups:
        record = group["record"]
        model = _model_from_record(record)
        if model != "unknown":
            carried_model = model
        else:
            model = carried_model or "unknown"
        merged.append({
            **record,
            "model": model,
            "identity": _stable_identity(group["keys"]),
            "source_idx": group["source_idx"],
        })
    return merged


def _resolve_usage_timestamp(record: dict, timestamps: dict[int, str]) -> str | None:
    """优先记录自带 createdAt;缺失(没有 9.4 的消息)时按同 idx 的 step 回退。"""
    value = record.get("timestamp")
    if value:
        return value
    idx = record.get("idx")
    if idx is None:
        return None
    return timestamps.get(idx)


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
            # language server 序列化的是与 .db 行相同的 GeneratorMetadata
            # protobuf:outputTokens(字段 3)是总量,thinking 是其中一部分,
            # 按输出价计一次,不能计两次。
            total_output = safe_float(usage.get("outputTokens"))
            thinking = safe_float(usage.get("thinkingOutputTokens"))
            output_tokens = max(0, total_output - thinking)
            event = make_event(
                kind=KIND,
                source_key="response" if response_id else item.state_key,
                ordinal=response_id or len(events),
                model=_legacy_model(chat_model),
                requested_at=requested_at,
                # request_log 没有 cache-creation 列,按本仓库惯例折入 input,
                # 与 .db 路径同一口径。
                input_tokens=(safe_float(usage.get("inputTokens"))
                              + safe_float(usage.get("cacheCreationTokens"))),
                output_tokens=output_tokens,
                cached_input_tokens=safe_float(usage.get("cacheReadTokens")),
                reasoning_output_tokens=max(thinking, total_output - output_tokens),
                project=project,
                session_id=item.path.stem,
            )
            if event:
                events.append(event)
    return batch(events, len(metadata))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    if item.context.get("legacy"):
        return _parse_legacy(item, stop_event)
    db = item.path
    if not db.is_file():
        return batch([], 0)
    strict = bool(item.context.get("strict"))
    try:
        # 一次 steps 扫描同时拿到:只落在 step 上的用量、
        # gen_metadata 行缺失自带时间戳时的 idx → 时钟回退、会话事件。
        step_rows = _read_step_rows(db, strict=strict)
        records = _read_usage_records(db)
    except (OSError, sqlite3.Error):
        return batch(
            [], 0, skipped=True,
            warnings=(
                "antigravity: 会话数据库读取失败，已保留上次同步状态。",
            ),
        )
    workspace = _workspace(db)
    merged = _merge_cascade_usage(records, step_rows["usages"])
    record_count = len(records) + len(step_rows["usages"])
    events = []
    for index, record in enumerate(merged):
        if stop_event is not None and stop_event.is_set():
            return batch(events, record_count, skipped=True)
        # 时钟要么来自记录自身,要么来自同一 idx 的 steps.metadata
        # (generator 行可以没有 chatStartMetadata,时间戳此时按 idx 回退)。
        requested_at = _resolve_usage_timestamp(record, step_rows["timestamps"])
        if requested_at is None:
            continue
        # event_id 用组级身份而不是胜出副本的字段:同一调用换一份更丰富的
        # 副本时,增量导入的去重凭据保持不变。
        identity = record.get("identity")
        if identity:
            source_key, _, ordinal = identity.partition(":")
        else:
            source_key = item.state_key
            ordinal = record.get("source_idx")
            if ordinal is None:
                ordinal = index
        event = make_event(
            kind=KIND,
            source_key=source_key,
            ordinal=ordinal,
            model=record.get("model"),
            requested_at=requested_at,
            # request_log 没有 cache-creation 列,按本仓库惯例折入 input。
            input_tokens=record["input"] + record["cache_creation"],
            output_tokens=record["output"],
            cached_input_tokens=record["cache_read"],
            reasoning_output_tokens=record["thinking"],
            project=project_name(workspace),
            session_id=db.stem,
        )
        if event:
            events.append(event)
    return batch(events, record_count)
