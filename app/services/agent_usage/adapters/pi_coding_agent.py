"""Official Pi coding agent adapter."""

from pathlib import Path

from ..cindy_ledger import discover as discover_cindy, parse as parse_cindy
from ..ir import ParseBatch, UsageSource
from .pi_common import discover_pi, parse_pi

KIND = "pi-coding-agent"
LABEL = "pi"
DEFAULT_PATH = Path.home() / ".pi" / "agent" / "sessions"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return discover_pi(software, KIND) + discover_cindy(software, KIND)


def parse(item: UsageSource, stop_event=None, **kwargs) -> ParseBatch:
    if item.context.get("cindy"):
        return parse_cindy(item, KIND)
    return parse_pi(item, KIND, stop_event)
