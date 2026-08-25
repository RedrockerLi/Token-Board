"""Reconcile the V1 dashboard account mirror from the proxy database.

Historical table repair belongs to the offline schema transition package.  The
running application only ever sees the normalized V1 tables.
"""

from app.core import sqlite_runtime


def reconcile_accounts(dash_path: str, proxy_path: str) -> None:
    """Synchronize the dashboard's stable account names from proxy V1.

    The operation is intentionally idempotent and preserves soft-deleted
    accounts so historical dashboard rows retain their display name.
    """
    proxy = sqlite_runtime.connect(proxy_path, "proxy_runtime")
    dash = sqlite_runtime.connect(dash_path, "dashboard_runtime")
    try:
        accounts = proxy.execute(
            "SELECT id,name,lifecycle_state,updated_at,account_kind "
            "FROM accounts ORDER BY id"
        ).fetchall()
        dash.executemany(
            "INSERT INTO accounts(account_id,name,lifecycle_state,updated_at,account_kind) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "name=excluded.name,lifecycle_state=excluded.lifecycle_state,"
            "updated_at=excluded.updated_at,account_kind=excluded.account_kind",
            [(row["id"], row["name"], row["lifecycle_state"],
              row["updated_at"], row["account_kind"]) for row in accounts],
        )
        dash.commit()
    finally:
        proxy.close()
        dash.close()
