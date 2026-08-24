"""WorkBuddy JSONL adapter."""

import os
from pathlib import Path

from ..common import batch, config_value, iter_jsonl, make_event, project_name, source, timestamp, walk_files
from ..ir import ParseBatch, UsageSource

KIND = "workbuddy"
LABEL = "WorkBuddy"
DEFAULT_PATH = Path.home() / ".workbuddy-ai" / "projects"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = config_value(software, "data_root", "path") or os.environ.get("VIBE_USAGE_WORKBUDDY_DIRS")
    roots = [Path(value).expanduser() for value in configured.split(os.pathsep)] if configured else [
        Path.home() / ".workbuddy-ai" / "projects",
        Path.home() / ".workbuddy" / "projects",
    ]
    out = []
    for value in roots:
        projects = value if value.name == "projects" else value / "projects"
        out.extend(source(path, projects_root=projects)
                   for path in walk_files(projects, (".jsonl",)))
    return list(dict.fromkeys(out))


def _number(value):
    try:
        return max(0, float(value or 0))
    except (TypeError, ValueError):
        return 0


def _detail(value, *keys):
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, dict):
            for key in keys:
                if item.get(key) is not None:
                    return _number(item[key])
    return 0


def _role(record: dict) -> str | None:
    role = record.get("role")
    if role is None and isinstance(record.get("message"), dict):
        role = record["message"].get("role")
    if role == "user":
        return "user"
    if role in {"assistant", "assistant_message"}:
        return "assistant"
    return None


def _completed_assistant(record: dict) -> bool:
    if record.get("type") != "message" or _role(record) != "assistant":
        return False
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    status = str(
        record.get("status")
        or message.get("status")
        or record.get("state")
        or message.get("state")
        or ""
    ).lower()
    return status in {"completed", "complete", "success"}


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    events = []
    count = 0
    projects_root = item.context.get("projects_root")
    try:
        relative = item.path.relative_to(Path(projects_root)) if projects_root else None
        project = project_name(relative.parts[0]) if relative and relative.parts else "unknown"
    except (TypeError, ValueError):
        project = "unknown"
    for line_no, record in iter_jsonl(item.path):
        count = line_no
        if not isinstance(record, dict):
            continue
        provider = record.get("providerData") if isinstance(record.get("providerData"), dict) else {}
        is_function_call = record.get("type") == "function_call" and bool(provider)
        if not (_completed_assistant(record) or is_function_call):
            continue
        primary = provider.get("usage") if isinstance(provider.get("usage"), dict) else (record.get("message", {}).get("usage") if isinstance(record.get("message"), dict) and isinstance(record.get("message", {}).get("usage"), dict) else None)
        raw = provider.get("rawUsage") if isinstance(provider.get("rawUsage"), dict) else {}
        if not primary and not raw:
            continue
        input_details = (primary or {}).get("input_details") or (primary or {}).get("inputDetails") or (primary or {}).get("inputTokensDetails") or raw.get("prompt_tokens_details")
        output_details = (primary or {}).get("output_details") or (primary or {}).get("outputDetails") or (primary or {}).get("outputTokensDetails") or raw.get("completion_tokens_details")
        cache = _detail(input_details, "cached_tokens", "cachedTokens") or _number((primary or {}).get("cachedInputTokens") or (primary or {}).get("cacheReadInputTokens") or raw.get("prompt_cache_hit_tokens") or raw.get("cache_read_input_tokens"))
        reasoning = _detail(output_details, "reasoning_tokens", "reasoningTokens") or _number((primary or {}).get("reasoningOutputTokens") or (primary or {}).get("reasoning_tokens") or (primary or {}).get("reasoningTokens") or raw.get("completion_thinking_tokens"))
        inclusive_input = _number((primary or {}).get("inputTokens") or (primary or {}).get("input_tokens") or raw.get("prompt_tokens"))
        inclusive_output = _number((primary or {}).get("outputTokens") or (primary or {}).get("output_tokens") or raw.get("completion_tokens"))
        cache_miss = _number(raw.get("prompt_cache_miss_tokens"))
        ident = record.get("id")
        session_id = str(record.get("sessionId") or record.get("session_id") or item.path.stem)
        ordinal = ident if ident is not None else f"{item.path}:{line_no}"
        stable_source = session_id if ident is not None else item.state_key
        model = provider.get("requestModelId") or record.get("requestModelName") or provider.get("requestModelName") or provider.get("model") or "unknown"
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        raw_timestamp = (
            record.get("completedAt") or record.get("completed_at")
            or record.get("timestamp") or record.get("createdAt")
            or record.get("created_at") or message.get("createdAt")
            or message.get("created_at")
        )
        event = make_event(
            kind=KIND, source_key=stable_source, ordinal=ordinal, model=model,
            requested_at=timestamp(raw_timestamp),
            input_tokens=cache_miss or max(0, inclusive_input - cache), output_tokens=max(0, inclusive_output - reasoning),
            cached_input_tokens=cache, reasoning_output_tokens=reasoning,
            project=project_name(record.get("cwd") or project), session_id=session_id,
        )
        if event:
            events.append(event)
    return batch(events, count)
