"""Keep pre-V1.4 dashboard agent archives on the proxy's unified ids."""

from pathlib import Path
import sqlite3
from app.db.migrations import migrate


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def align_dashboard_agent_ids(dashboard: Path, proxy: Path) -> None:
    if not dashboard.exists() or not proxy.exists():
        return
    dash = sqlite3.connect(dashboard, timeout=10)
    proxy_conn = sqlite3.connect(proxy)
    try:
        if not _table_exists(dash, "agent_software") or not _table_exists(
                dash, "agent_daily_usage"):
            return
        dash.row_factory = sqlite3.Row
        proxy_conn.row_factory = sqlite3.Row
        agents = {(row["name"], row["agent_kind"]): int(row["id"])
                  for row in proxy_conn.execute(
                      "SELECT id,name,agent_kind FROM agent_software")}
        rows = dash.execute(
            "SELECT software_id,name,agent_kind FROM agent_software").fetchall()
        mapping = {}
        for row in rows:
            new_id = agents.get((row["name"], row["agent_kind"]))
            if new_id is not None and int(row["software_id"]) != new_id:
                mapping[int(row["software_id"])] = new_id
        if not mapping:
            return
        dash.execute("PRAGMA foreign_keys=OFF")
        for old_id, new_id in mapping.items():
            dash.execute("UPDATE agent_daily_usage SET software_id=? WHERE software_id=?",
                         (new_id, old_id))
            dash.execute("UPDATE agent_software SET software_id=? WHERE software_id=?",
                         (new_id, old_id))
        if dash.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("dashboard agent-id alignment FK check failed")
        dash.commit()
        dash.execute("PRAGMA foreign_keys=ON")
    finally:
        proxy_conn.close()
        dash.close()


def prepare_dashboard_agent_alignment(dashboard: Path, proxy: Path,
                                      schema_root: Path) -> None:
    """Align a pre-V1.4 archive after bringing the proxy to its final ids."""
    version = None
    if proxy.exists():
        conn = sqlite3.connect(proxy)
        try:
            row = conn.execute(
                "SELECT major,minor FROM schema_version WHERE id=1"
            ).fetchone()
            version = (int(row[0]), int(row[1])) if row else None
        finally:
            conn.close()
    if version is not None and version[0] == 1:
        migrate(str(proxy), str(schema_root), "proxy")
        align_dashboard_agent_ids(dashboard, proxy)
