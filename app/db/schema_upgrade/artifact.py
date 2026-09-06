"""Sanitizers for downloaded, configuration-only database artifacts."""

from __future__ import annotations

from pathlib import Path

from app.core import sqlite_runtime
from app.db.migrations import TOKEN_BOARD_DATABASE_NAME


def strip_runtime_artifact(path: Path, database_name: str) -> None:
    """Remove machine-local state before a config artifact is merged."""
    if database_name != TOKEN_BOARD_DATABASE_NAME:
        return
    runtime_tables = (
        "request_log", "request_attempts", "billing_period_charges",
        "billing_export_events",
        "agent_subscription_period_charges", "agent_subscription_charge_allocations",
        "agent_software_runtime",
        "fx_rates", "sync_state",
    )
    conn = sqlite_runtime.connect(path, "snapshot_restore")
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
