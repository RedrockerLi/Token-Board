"""Qoder CN usage adapter (China edition)."""

from __future__ import annotations

from pathlib import Path

from .qoder import (
    discover_qoder,
    parse as parse_qoder,
)
from ..ir import ParseBatch, UsageSource

KIND = "qoder-cn"
LABEL = "Qoder CN"
DESCRIPTION = "Qoder CN 本地用量"
DEFAULT_PATH = Path.home() / ".qoder-cn" / "projects"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return discover_qoder(software, edition="qoder-cn", stop_event=stop_event)


def parse(item: UsageSource, stop_event=None, **kwargs) -> ParseBatch:
    # Ensure edition context is set to qoder-cn
    if not item.context.get("edition"):
        item = UsageSource(path=item.path, key=item.key, context={**item.context, "edition": "qoder-cn"})
    return parse_qoder(item, stop_event=stop_event, **kwargs)
