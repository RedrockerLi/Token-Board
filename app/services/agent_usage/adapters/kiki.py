"""Kiki usage adapter.

Kiki is a separate tool with its own data home, but it speaks the same
per-agent Kimi wire protocol, so the record reader is shared with
``kimi_code``.  Two behaviours are specific to the Kiki identity: model ids
have known routing/provider prefixes stripped before pricing, and a Kiki home
that physically resolves to a Kimi Code home is never collected twice.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..common import (
    batch,
    config_value,
    make_event,
    safe_int,
    source,
    timestamp,
)
from ..ir import ParseBatch, UsageSource
from . import kimi_code

KIND = "kiki"
LABEL = "Kiki"
DESCRIPTION = "Kiki 本地会话用量"
DEFAULT_PATH = Path.home() / ".kiki"

# Strip only known routing/provider prefixes, never arbitrary slash-bearing
# model ids (e.g. Qwen/Qwen3.8-Flash).  Nested known routes are stripped in
# order, outermost first.
KIKI_MODEL_PREFIXES = (
    "axon", "axon-message", "axon-chat", "kimi-code", "anthropic",
    "deepseek", "z-ai", "hub", "stealth", "st", "minimax",
)


def normalize_kiki_model(model, prefixes=KIKI_MODEL_PREFIXES) -> str:
    """Strip known routing prefixes from one wire record's model id."""
    if not isinstance(model, str) or not model:
        return "unknown"
    normalized = model
    while True:
        slash = normalized.find("/")
        if (slash < 0 or slash == len(normalized) - 1
                or normalized[:slash] not in prefixes):
            return normalized
        normalized = normalized[slash + 1:]


def _roots(software: dict) -> list[Path]:
    """Kiki data homes; one home per installation, overrides replace it."""
    configured = config_value(software, "data_root", "path")
    if configured:
        return [Path(configured).expanduser()]
    override = (os.environ.get("VIBE_USAGE_KIKI_DIR", "").strip()
                or os.environ.get("KIKI_HOME", "").strip())
    return [Path(override).expanduser() if override else DEFAULT_PATH]


def _identity(path: Path) -> Path:
    """Physical identity of a root; a root that does not exist is its own."""
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def independent_roots(roots: list[Path], exclude_roots: list[Path]) -> list[Path]:
    """Drop roots that name a home another identity already owns.

    Kiki and Kimi Code can be pointed at one physical home by an override;
    counting that home under both identities would double every record.
    """
    seen = {_identity(root) for root in exclude_roots}
    result = []
    for root in roots:
        key = _identity(root)
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result


def _kimi_code_roots(software: dict) -> list[Path]:
    """Kimi Code homes, for the independence check above.

    The reference parser imports ``resolveKimiCodeRoots`` for this; reuse the
    Kimi Code adapter's own resolver so both identities cannot disagree about
    where Kimi Code lives.
    """
    current, _legacy = kimi_code._roots(software)
    return list(current)


def _sessions_dir(root: Path) -> Path:
    return root if root.name == "sessions" else root / "sessions"


def _session_dir(path: Path) -> Path:
    # <sessions>/<work dir>/<session>/agents/<agent>/wire.jsonl
    parents = path.parents
    return parents[2] if len(parents) > 2 else path.parent


def _project(root: Path, session_dir: Path) -> str:
    """Project name for one session directory, like the shared reader.

    The session index names the work directory; the bucket directory name is
    the fallback, then ``unknown``.
    """
    index = kimi_code._load_index(root)
    project = (index.get(str(session_dir))
               or index.get(str(_identity(session_dir))))
    return project or kimi_code._project_from_bucket(session_dir.parent.name)


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    roots = independent_roots(_roots(software), _kimi_code_roots(software))
    out = []
    seen = set()
    failures: list[tuple[Path, str]] = []
    for root in roots:
        for path in kimi_code._wire_files(_sessions_dir(root), failures):
            if stop_event is not None and stop_event.is_set():
                return list(dict.fromkeys(out))
            identity = _identity(path)
            if identity in seen:
                continue
            seen.add(identity)
            out.append(source(
                path, layout="current", session_dir=_session_dir(path), root=root,
            ))
    if failures:
        # Unreadable paths get their own warning source so their cursors never
        # advance; the readable sources above still import, like the reference
        # parser, which keeps what it could read and only marks the result
        # skipped.
        out.append(source(
            failures[0][0],
            key="kiki-discovery",
            discovery_warnings=tuple(dict.fromkeys(
                message for _, message in failures)),
        ))
    return list(dict.fromkeys(out))


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    discovery_warnings = tuple(item.context.get("discovery_warnings") or ())
    if discovery_warnings:
        return batch([], 0, skipped=True, warnings=discovery_warnings)
    root = Path(item.context.get("root") or _session_dir(item.path))
    session_dir = Path(item.context.get("session_dir") or _session_dir(item.path))
    project = _project(root, session_dir)
    # Main and sub-agent wires share one logical session directory but each
    # carries independent usage deltas, so the agent directory is part of the
    # source identity.
    source_key = f"current:{session_dir}:{item.path.parent.name}"
    events = []
    count = 0
    try:
        for line_no, raw in kimi_code._wire_records(item.path):
            count = line_no
            if stop_event is not None and stop_event.is_set():
                return batch(events, count, skipped=True)
            if raw.get("type") != "usage.record":
                continue
            usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            event = make_event(
                kind=KIND,
                source_key=source_key,
                ordinal=raw.get("id") or line_no,
                model=normalize_kiki_model(raw.get("model")),
                requested_at=timestamp(raw.get("time")),
                # Cache creation is billed as non-cached input, matching the
                # common bucket model; cache reads stay in their own field.
                input_tokens=(safe_int(usage.get("inputOther"))
                              + safe_int(usage.get("inputCacheCreation"))),
                output_tokens=usage.get("output", 0),
                cached_input_tokens=usage.get("inputCacheRead", 0),
                project=project,
                session_id=str(session_dir),
            )
            if event:
                events.append(event)
    except OSError as exc:
        # Whatever parsed before the failure is kept, but the batch is marked
        # skipped so the importer neither advances this file's cursor nor
        # treats the unread records as gone.
        return batch(events, count, skipped=True,
                     warnings=(kimi_code._read_failure(item.path, exc),))
    return batch(events, count)
