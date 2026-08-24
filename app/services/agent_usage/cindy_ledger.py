"""Cindy's durable daily ledger used by the Codex and Pi harnesses.

Cindy is not a separate Vibe Usage parser.  Its Claude SDK integration writes
ordinary Claude transcripts, while its Codex and Pi integrations may only be
represented in ``daily_model_usage``.  Keep this as a source helper and merge
it into those two agent adapters so the public registry remains aligned with
the actual agent kinds.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import batch, config_value, make_event, safe_int, source, sqlite_rows
from .ir import ParseBatch, UsageSource


_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CINDY_DB_RE = re.compile(r"^cindy-.+\.db$")

_SQL = """
SELECT day, agent_kind, model,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(cache_create_tokens) AS cache_create_tokens
FROM daily_model_usage
GROUP BY day, agent_kind, model
ORDER BY day, agent_kind, model
"""


def _roots(software: dict | None = None) -> list[Path]:
    configured = config_value(software or {}, "cindy_dirs", "cindy_data_root")
    override = configured or os.environ.get("VIBE_USAGE_CINDY_DIRS", "")
    if override.strip():
        return [Path(value).expanduser() for value in override.split(os.pathsep)
                if value.strip()]

    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [base / "CindyGlobal", base / "Cindy"]


def find_db_paths(software: dict | None = None) -> list[Path]:
    """Find active owner databases, ignoring backups and other artifacts."""
    paths = []
    seen = set()
    for root in _roots(software):
        root = root.resolve() if root.exists() else root
        if root.is_file():
            candidates = [root] if root.name.endswith(".db") else []
        elif root.is_dir():
            try:
                candidates = [child for child in root.iterdir()
                              if child.is_file() and _CINDY_DB_RE.match(child.name)]
            except OSError:
                candidates = []
        else:
            candidates = []
        for path in candidates:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return sorted(paths, key=lambda path: str(path))


def discover(software: dict | None, kind: str) -> list[UsageSource]:
    """Return Cindy ledger DBs as sources for one supported harness."""
    return [source(
        path,
        key=f"cindy:{kind}:{path}",
        cindy=True,
        cindy_kind=kind,
    ) for path in find_db_paths(software)]


def _day_timestamp(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = _DAY_RE.match(value.strip())
    if not match:
        return None
    try:
        # Cindy stores a calendar day in the user's local timezone. Match the
        # reference parser's local-midnight semantics before normalizing the
        # timestamp written to request_log as UTC.
        date = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
        ).astimezone(timezone.utc)
    except ValueError:
        return None
    return date.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(item: UsageSource, kind: str) -> ParseBatch:
    ledger_kind = "pi" if kind == "pi-coding-agent" else kind
    rows = sqlite_rows(item.path, _SQL)
    events = []
    for row in rows:
        if str(row["agent_kind"] or "") != ledger_kind:
            continue
        requested_at = _day_timestamp(row["day"])
        model = str(row["model"] or f"{kind}-unknown").strip() or f"{kind}-unknown"
        input_tokens = safe_int(row["input_tokens"]) + safe_int(row["cache_create_tokens"])
        output_tokens = safe_int(row["output_tokens"])
        cached = safe_int(row["cache_read_tokens"])
        event = make_event(
            kind=kind,
            source_key=item.state_key,
            ordinal=f"{row['day']}:{model}",
            model=model,
            requested_at=requested_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            # Cindy's aggregate total follows the reference bucket model:
            # cache reads are priced separately and are not part of total.
            total_tokens=input_tokens + output_tokens,
            project="unknown",
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
