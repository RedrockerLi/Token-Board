"""Sanitizers for downloaded, configuration-only database artifacts."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def strip_runtime_artifact(path: Path, database_name: str) -> None:
    """Remove machine-local state before a config artifact is merged."""
    if database_name != "proxy":
        return
    runtime_tables = (
        "request_log", "request_attempts", "billing_period_charges",
        "agent_subscription_period_charges", "agent_software_runtime",
        "fx_rates", "sync_state",
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in runtime_tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists:
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()
