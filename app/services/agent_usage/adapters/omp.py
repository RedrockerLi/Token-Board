"""Oh My Pi adapter."""

from pathlib import Path

from ..ir import ParseBatch, UsageSource
from .pi_common import discover_pi, parse_pi

KIND = "omp"
LABEL = "Oh My Pi"
DEFAULT_PATH = Path.home() / ".omp" / "agent" / "sessions"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    return discover_pi(software, KIND)


def parse(item: UsageSource, stop_event=None, **kwargs) -> ParseBatch:
    return parse_pi(item, KIND, stop_event)
