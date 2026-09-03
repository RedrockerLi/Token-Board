"""One-time cleanup of the legacy agent billing mirror."""

from __future__ import annotations

import sqlite3

from app.db.schema_upgrade.transition_api import TransitionContext


TRANSITION_ID = "v1-legacy-agent-billing"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def apply(context: TransitionContext) -> None:
    token_board = sqlite3.connect(context.shadow("token-board"))
    dashboard = sqlite3.connect(context.shadow("dashboard"))
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


def verify(context: TransitionContext) -> dict:
    p = sqlite3.connect(context.shadow("token-board"))
    d = sqlite3.connect(context.shadow("dashboard"))
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
        return {"legacy_accounts": len(rows)}
    finally:
        d.close()
        p.close()
