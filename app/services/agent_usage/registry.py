"""Registry for every local agent adapter."""

from __future__ import annotations

from pathlib import Path

from .adapters import alma, amp, antigravity, claude_code, cline, codex
from .adapters import copilot_cli, craft_agent, cursor, dimagent, droid, dsh
from .adapters import gemini_cli, grok, hermes, kimi_code, kiro, mimocode
from .adapters import omp, openclaw, opencode, pi_coding_agent, qwen_code
from .adapters import roo_code, trae_cli, workbuddy, zcode

ADAPTERS = {
    module.KIND: module for module in (
        claude_code, codex, grok, copilot_cli, craft_agent, cursor, dimagent,
        gemini_cli, opencode, openclaw, omp, pi_coding_agent, qwen_code,
        kimi_code, amp, alma, droid, dsh, antigravity, trae_cli, hermes,
        kiro, mimocode, cline, roo_code, workbuddy, zcode,
    )
}


def _under_home(value) -> bool:
    try:
        Path(value).relative_to(Path.home())
        return True
    except (ValueError, TypeError):
        return False


AGENT_TYPES = {
    kind: {
        "label": module.LABEL,
        "description": getattr(module, "DESCRIPTION", f"{module.LABEL} 本地用量"),
        "default_path": getattr(module, "DEFAULT_PATH_DISPLAY", None) or (
            "~/" + str(Path(module.DEFAULT_PATH).relative_to(Path.home()))
            if _under_home(module.DEFAULT_PATH) else str(module.DEFAULT_PATH)
        ),
    }
    for kind, module in ADAPTERS.items()
}


def get_adapter(kind: str):
    return ADAPTERS.get(str(kind or "").strip().lower())
