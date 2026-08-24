"""Cross-database V1 agent identity transition.

The dashboard V1.3 archive keyed agent rows by a local software id.  The
token-board V1.8 schema moved those identities into the shared account id
namespace and may have remapped colliding ids.  This transform therefore has
to run between the Token Board and Dashboard SQL steps and must operate on
shadows of both databases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.migrations import (TOKEN_BOARD_DATABASE_NAME, SchemaVersion,
                               apply_sql_migrations)


TRANSITION_ID = "v1-agent-identity"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_transition(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "schema_transitions"):
        return False
    return conn.execute(
        "SELECT 1 FROM schema_transitions WHERE transition_id=?",
        (TRANSITION_ID,),
    ).fetchone() is not None


def _token_board_agent_rows(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    return [tuple(row) for row in conn.execute(
        "SELECT id,name,agent_kind FROM agent_software ORDER BY id"
    )]


def _legacy_needs_alignment(dashboard: Path, token_board: Path) -> bool:
    dash = sqlite3.connect(dashboard)
    token_board_conn = sqlite3.connect(token_board)
    try:
        if not _table_exists(dash, "agent_software"):
            return False
        if not _table_exists(token_board_conn, "agent_software"):
            return False
        if _has_transition(dash) and _has_transition(token_board_conn):
            return False
        token_board_rows = _token_board_agent_rows(token_board_conn)
        by_identity = {(name, kind): ident
                       for ident, name, kind in token_board_rows}
        dashboard_rows = [tuple(row) for row in dash.execute(
            "SELECT software_id,name,agent_kind FROM agent_software"
        )]
        if len(by_identity) != len(token_board_rows):
            raise RuntimeError("token-board agent identities are not unique")
        seen = set()
        for old_id, name, kind in dashboard_rows:
            key = (name, kind)
            if key in seen or key not in by_identity:
                raise RuntimeError(
                    f"dashboard agent identity is missing or ambiguous: {key!r}")
            seen.add(key)
            if int(old_id) != int(by_identity[key]):
                return True
        return bool(dashboard_rows)
    finally:
        token_board_conn.close()
        dash.close()


def _generic_needs_repair(dashboard: Path, token_board: Path) -> bool:
    dash = sqlite3.connect(dashboard)
    token_board_conn = sqlite3.connect(token_board)
    try:
        if not (_table_exists(dash, "accounts") and
                _table_exists(token_board_conn, "agent_software")):
            return False
        token_board_rows = _token_board_agent_rows(token_board_conn)
        token_board_ids = {
            name: ident for ident, name, _kind in token_board_rows}
        rows = dash.execute(
            "SELECT account_id,name FROM accounts WHERE account_kind='agent'"
        ).fetchall()
        if len(token_board_ids) != len(token_board_rows):
            raise RuntimeError("token-board agent names are not unique")
        for ident, name in rows:
            if name not in token_board_ids:
                raise RuntimeError(f"dashboard agent identity is unknown: {name!r}")
            if int(ident) != int(token_board_ids[name]):
                return True
        return False
    finally:
        token_board_conn.close()
        dash.close()


def needs(token_board: Path, dashboard: Path,
          token_board_version: SchemaVersion | None,
          dashboard_version: SchemaVersion | None) -> bool:
    """Return whether this transition must run for the current pair."""
    if token_board_version is None or dashboard_version is None:
        return False
    if token_board_version.major != 1 or dashboard_version.major != 1:
        return False
    if dashboard_version.minor >= 4:
        # A previously bypassed V1.4 SQL upgrade can still leave account ids
        # stale.  The same transform repairs that layout when needed.
        dash = sqlite3.connect(dashboard)
        try:
            if not _table_exists(dash, "accounts"):
                return False
            if not _table_exists(dash, "daily_usage"):
                return False
            # Token Board V1.8 is the point where agent ids enter the shared account
            # namespace and may be remapped.  If the dashboard already ran
            # V1.4 while Token Board is still pre-V1.8, equal ids are not proof
            # of safety: the upcoming Token Board SQL can change them.  Force the
            # pair through the shadow barrier before that SQL is published.
            if token_board_version.minor < 8 and dash.execute(
                    "SELECT 1 FROM accounts WHERE account_kind='agent' LIMIT 1"
            ).fetchone():
                return True
            if not _table_exists(dash, "schema_transitions"):
                return _generic_needs_repair(dashboard, token_board)
            return not _has_transition(dash) and _generic_needs_repair(
                dashboard, token_board)
        finally:
            dash.close()
    return _legacy_needs_alignment(dashboard, token_board)


def _update_legacy_archive(dashboard: sqlite3.Connection,
                           token_board: sqlite3.Connection) -> None:
    """Map V1.3 agent tables using collision-safe temporary ids."""
    token_board_rows = _token_board_agent_rows(token_board)
    by_identity = {(name, kind): ident
                   for ident, name, kind in token_board_rows}
    rows = [tuple(row) for row in dashboard.execute(
        "SELECT software_id,name,agent_kind FROM agent_software ORDER BY software_id"
    )]
    mapping = []
    for index, (old_id, name, kind) in enumerate(rows, start=1):
        key = (name, kind)
        if key not in by_identity:
            raise RuntimeError(f"cannot map dashboard agent identity: {key!r}")
        mapping.append((int(old_id), int(by_identity[key]), -2_000_000_000 - index))
    if len({row[1] for row in mapping}) != len(mapping):
        raise RuntimeError(
            "token-board agent identities map to duplicate account ids")

    dashboard.execute("PRAGMA foreign_keys=OFF")
    dashboard.execute(
        "CREATE TEMP TABLE agent_transition_map(old_id INTEGER PRIMARY KEY, "
        "new_id INTEGER NOT NULL UNIQUE, temp_id INTEGER NOT NULL UNIQUE)"
    )
    dashboard.executemany(
        "INSERT INTO agent_transition_map(old_id,new_id,temp_id) VALUES(?,?,?)",
        mapping,
    )
    dashboard.execute(
        "UPDATE agent_daily_usage SET software_id=("
        "SELECT temp_id FROM agent_transition_map WHERE old_id=software_id)"
        " WHERE software_id IN (SELECT old_id FROM agent_transition_map)"
    )
    dashboard.execute(
        "UPDATE agent_software SET software_id=("
        "SELECT temp_id FROM agent_transition_map WHERE old_id=software_id)"
        " WHERE software_id IN (SELECT old_id FROM agent_transition_map)"
    )
    dashboard.execute(
        "UPDATE agent_daily_usage SET software_id=("
        "SELECT new_id FROM agent_transition_map WHERE temp_id=software_id)"
        " WHERE software_id IN (SELECT temp_id FROM agent_transition_map)"
    )
    dashboard.execute(
        "UPDATE agent_software SET software_id=("
        "SELECT new_id FROM agent_transition_map WHERE temp_id=software_id)"
        " WHERE software_id IN (SELECT temp_id FROM agent_transition_map)"
    )
    dashboard.execute("DROP TABLE agent_transition_map")
    dashboard.commit()
    dashboard.execute("PRAGMA foreign_keys=ON")


def _repair_v14_archive(dashboard: sqlite3.Connection,
                        token_board: sqlite3.Connection) -> None:
    """Repair a generic V1.4 archive that bypassed the transition barrier."""
    token_board_rows = _token_board_agent_rows(token_board)
    by_name = {name: ident for ident, name, _kind in token_board_rows}
    if len(by_name) != len(token_board_rows):
        raise RuntimeError("token-board agent names are not unique")
    rows = [tuple(row) for row in dashboard.execute(
        "SELECT account_id,name FROM accounts WHERE account_kind='agent'"
    )]
    mapping = []
    for index, (old_id, name) in enumerate(rows, start=1):
        if name not in by_name:
            raise RuntimeError(f"cannot map dashboard agent account: {name!r}")
        new_id = int(by_name[name])
        if int(old_id) != new_id:
            mapping.append((int(old_id), new_id, -2_100_000_000 - index))
    if not mapping:
        return
    if len({row[1] for row in mapping}) != len(mapping):
        raise RuntimeError("dashboard agent repair maps to duplicate ids")

    old_ids = {old_id for old_id, _new_id, _temp_id in mapping}
    # Existing rows at the target id are a real identity collision unless that
    # row is itself part of this swap.  Never merge unrelated usage silently.
    for _old_id, new_id, _temp_id in mapping:
        if dashboard.execute(
                "SELECT 1 FROM accounts WHERE account_id=?",
                (new_id,)).fetchone():
            if new_id not in old_ids:
                raise RuntimeError(f"dashboard account id collision at {new_id}")

    dashboard.execute("PRAGMA foreign_keys=OFF")
    dashboard.execute(
        "CREATE TEMP TABLE agent_transition_map(old_id INTEGER PRIMARY KEY, "
        "new_id INTEGER NOT NULL UNIQUE, temp_id INTEGER NOT NULL UNIQUE)"
    )
    dashboard.executemany(
        "INSERT INTO agent_transition_map(old_id,new_id,temp_id) VALUES(?,?,?)",
        mapping,
    )
    for table in ("daily_usage", "monthly_recurring_costs"):
        if _table_exists(dashboard, table):
            dashboard.execute(
                f"UPDATE {table} SET account_id=(SELECT temp_id FROM "
                "agent_transition_map WHERE old_id=account_id) WHERE account_id "
                "IN (SELECT old_id FROM agent_transition_map)"
            )
    dashboard.execute(
        "UPDATE accounts SET account_id=(SELECT temp_id FROM "
        "agent_transition_map WHERE old_id=account_id) WHERE account_id IN "
        "(SELECT old_id FROM agent_transition_map)"
    )
    for table in ("daily_usage", "monthly_recurring_costs"):
        if _table_exists(dashboard, table):
            dashboard.execute(
                f"UPDATE {table} SET account_id=(SELECT new_id FROM "
                "agent_transition_map WHERE temp_id=account_id) WHERE account_id "
                "IN (SELECT temp_id FROM agent_transition_map)"
            )
    dashboard.execute(
        "UPDATE accounts SET account_id=(SELECT new_id FROM "
        "agent_transition_map WHERE temp_id=account_id) WHERE account_id IN "
        "(SELECT temp_id FROM agent_transition_map)"
    )
    dashboard.execute("DROP TABLE agent_transition_map")
    dashboard.commit()
    dashboard.execute("PRAGMA foreign_keys=ON")


def apply(token_board_shadow: Path, dashboard_shadow: Path, schema_root: Path,
          token_board_version: SchemaVersion | None,
          dashboard_version: SchemaVersion | None) -> None:
    """Apply the complete transition to a pair of shadow databases."""
    if token_board_version is None or dashboard_version is None:
        return
    apply_sql_migrations(
        str(token_board_shadow), str(schema_root), TOKEN_BOARD_DATABASE_NAME)
    if dashboard_version.minor < 4:
        # Always materialize the legacy archive shape before applying V1.4.
        # A V1.0/V1.2 dashboard has no agent tables yet, but applying through
        # V1.3 is still the only safe way to make the cross-database barrier
        # explicit before the generic archive migration consumes that shape.
        apply_sql_migrations(
            str(dashboard_shadow), str(schema_root), "dashboard",
            SchemaVersion(1, 3))
        dash = sqlite3.connect(dashboard_shadow)
        token_board = sqlite3.connect(token_board_shadow)
        try:
            _update_legacy_archive(dash, token_board)
            dash.commit()
        finally:
            token_board.close()
            dash.close()
        apply_sql_migrations(str(dashboard_shadow), str(schema_root), "dashboard")
    else:
        dash = sqlite3.connect(dashboard_shadow)
        token_board = sqlite3.connect(token_board_shadow)
        try:
            _repair_v14_archive(dash, token_board)
            dash.commit()
        finally:
            token_board.close()
            dash.close()


def verify(token_board: Path, dashboard: Path) -> None:
    """Verify the post-transition identity invariant."""
    conn = sqlite3.connect(token_board)
    dash = sqlite3.connect(dashboard)
    try:
        token_board_ids = {
            name: ident for ident, name, _kind in _token_board_agent_rows(conn)}
        if _table_exists(dash, "accounts"):
            for ident, name in dash.execute(
                    "SELECT account_id,name FROM accounts WHERE account_kind='agent'"):
                if token_board_ids.get(name) != ident:
                    raise RuntimeError(
                        f"dashboard agent identity mismatch for {name!r}")
    finally:
        dash.close()
        conn.close()
