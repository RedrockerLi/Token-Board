"""CraftAgent Pi-session adapter."""

import os
from pathlib import Path

from ..common import config_value, source, walk_files
from ..ir import ParseBatch, UsageSource
from .pi_common import parse_pi

KIND = "craft-agent"
LABEL = "CraftAgent"
DEFAULT_PATH = Path.home() / ".craft-agent" / "workspaces"


def discover(software: dict, stop_event=None) -> list[UsageSource]:
    configured = config_value(software, "data_root", "path")
    if configured:
        root = Path(str(configured)).expanduser()
    else:
        agent_root = os.environ.get("CRAFT_AGENT_DIR") or os.environ.get("CRAFTAGENT_DIR")
        root = Path(agent_root).expanduser() if agent_root else DEFAULT_PATH.parent
        # The public CraftAgent override names the installation root, while a
        # direct config can point at the workspaces directory itself.
        if root.name != "workspaces":
            root = root / "workspaces"
    return [source(path) for path in walk_files(root, (".jsonl",)) if ".pi-sessions" in path.parts]


def parse(item: UsageSource, stop_event=None, **kwargs) -> ParseBatch:
    return parse_pi(item, KIND, stop_event)
