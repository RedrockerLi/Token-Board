"""DimAgent usage_ledger adapter."""

import json
import os
from pathlib import Path

from ..common import batch, config_value, make_event, project_name, safe_int, source, sqlite_rows, timestamp
from ..ir import ParseBatch, UsageSource

KIND = "dimagent"
LABEL = "DimAgent"
ALWAYS_SCAN = True
DEFAULT_PATH = Path.home() / ".dimcode" / "v2" / "dimcode.sqlite"


def _db(software: dict) -> Path:
    configured = config_value(software, "data_root", "path") or os.environ.get("VIBE_USAGE_DIMAGENT_DB") or os.environ.get("DIMCODE_HOME")
    if configured:
        path = Path(configured).expanduser()
        return path if path.suffix == ".sqlite" or path.suffix == ".db" else path / "dimcode.sqlite"
    return DEFAULT_PATH


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    path = _db(software)
    return [source(path)] if path.is_file() else []


def parse(item: UsageSource, stop_event=None, **_) -> ParseBatch:
    rows = sqlite_rows(item.path, """SELECT u.rowid AS usage_rowid, u.ledgerId,
        u.runId, u.providerId, u.modelId, u.usage, u.cost, u.createdAt, s.cwd
        FROM usage_ledger u LEFT JOIN sessions s ON s.sessionId=u.sessionId
        ORDER BY u.rowid""")
    original = {(
        row["runId"] or "", row["providerId"] or "", row["modelId"] or "",
        row["usage"] or "", row["cost"] or "", row["createdAt"] or ""
    ) for row in rows if not str(row["ledgerId"] or "").lower().startswith("ledger_")}
    orphan_seen = set()
    events = []
    for row in rows:
        ledger = str(row["ledgerId"] or "")
        signature = (
            row["runId"] or "", row["providerId"] or "", row["modelId"] or "",
            row["usage"] or "", row["cost"] or "", row["createdAt"] or "",
        )
        if ledger.lower().startswith("ledger_") and signature in original:
            continue
        if ledger.lower().startswith("ledger_"):
            if signature in orphan_seen:
                continue
            orphan_seen.add(signature)
        try:
            usage = json.loads(row["usage"]) if isinstance(row["usage"], str) else row["usage"]
        except (TypeError, ValueError):
            continue
        if not isinstance(usage, dict):
            continue
        cache = safe_int(usage.get("cacheReadTokens"))
        prompt = safe_int(usage.get("promptTokens"))
        event = make_event(
            kind=KIND, source_key=item.state_key, ordinal=row["usage_rowid"],
            model=row["modelId"] or "unknown", requested_at=timestamp(row["createdAt"]),
            input_tokens=max(0, prompt - cache), output_tokens=usage.get("completionTokens", 0),
            cached_input_tokens=cache, project=project_name(row["cwd"]),
        )
        if event:
            events.append(event)
    return batch(events, len(rows))
