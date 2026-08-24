"""One-time cleanup of the legacy agent billing mirror."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import SchemaVersion


TRANSITION_ID = "v1-legacy-agent-billing"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def needs(token_board: Path, dashboard: Path,
          token_board_version: SchemaVersion | None,
          dashboard_version: SchemaVersion | None) -> bool:
    if token_board_version is None or dashboard_version is None:
        return False
    if token_board_version.major != 1 or dashboard_version.major != 1:
        return False
    p = sqlite3.connect(token_board)
    d = sqlite3.connect(dashboard)
    try:
        if not (_table_exists(p, "account_importers") and
                _table_exists(p, "billing_contracts") and
                _table_exists(d, "monthly_recurring_costs")):
            return False
        legacy_ids = [row[0] for row in p.execute(
            "SELECT DISTINCT account_id FROM account_importers WHERE enabled=0 "
            "AND importer_kind IS NOT NULL"
        ).fetchall()]
        if not legacy_ids:
            return False
        placeholders = ",".join("?" for _ in legacy_ids)
        return d.execute(
            "SELECT 1 FROM monthly_recurring_costs WHERE recurring_charge<>0 "
            f"AND account_id IN ({placeholders}) LIMIT 1", legacy_ids
        ).fetchone() is not None
    finally:
        d.close()
        p.close()


def apply(token_board_shadow: Path, dashboard_shadow: Path, schema_root: Path,
          token_board_version: SchemaVersion | None,
          dashboard_version: SchemaVersion | None) -> None:
    del schema_root, token_board_version, dashboard_version
    token_board = sqlite3.connect(token_board_shadow)
    dashboard = sqlite3.connect(dashboard_shadow)
    try:
        rows = token_board.execute(
            "SELECT DISTINCT account_id FROM account_importers "
            "WHERE enabled=0 AND importer_kind IS NOT NULL"
        ).fetchall()
        has_normalized = _table_exists(dashboard, "monthly_recurring_costs") and \
            dashboard.execute(
                "SELECT 1 FROM pragma_table_info('monthly_recurring_costs') "
                "WHERE name='normalized_recurring_cost'"
            ).fetchone() is not None
        assignment = ("recurring_charge=0, normalized_recurring_cost=0"
                      if has_normalized else "recurring_charge=0")
        for (account_id,) in rows:
            dashboard.execute(
                f"UPDATE monthly_recurring_costs SET {assignment} "
                "WHERE account_id=?", (account_id,)
            )
        dashboard.commit()
    finally:
        dashboard.close()
        token_board.close()


def verify(token_board: Path, dashboard: Path) -> None:
    p = sqlite3.connect(token_board)
    d = sqlite3.connect(dashboard)
    try:
        rows = p.execute(
            "SELECT DISTINCT account_id FROM account_importers "
            "WHERE enabled=0 AND importer_kind IS NOT NULL"
        ).fetchall()
        has_normalized = _table_exists(d, "monthly_recurring_costs") and \
            d.execute(
                "SELECT 1 FROM pragma_table_info('monthly_recurring_costs') "
                "WHERE name='normalized_recurring_cost'"
            ).fetchone() is not None
        for (account_id,) in rows:
            condition = ("recurring_charge<>0 OR normalized_recurring_cost<>0"
                         if has_normalized else "recurring_charge<>0")
            if d.execute(
                f"SELECT 1 FROM monthly_recurring_costs WHERE account_id=? "
                f"AND ({condition}) LIMIT 1", (account_id,),
            ).fetchone():
                raise RuntimeError(
                    f"legacy agent recurring charge remains for account {account_id}")
    finally:
        d.close()
        p.close()
