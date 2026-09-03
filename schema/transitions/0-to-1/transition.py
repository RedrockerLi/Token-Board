"""Descriptor-driven V0 to V1 transition adapter.

The historical transformer remains split into focused implementation files;
this module is the small plugin interface used by the schema-upgrade runner.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# The application can load schema trees copied into different temporary
# directories during one process (notably cloud-artifact tests).  The legacy
# CLI modules use sibling top-level imports, so discard a previous copied
# module before importing this tree's implementation.
for _module_name in ("migrate", "transition_common", "transition_runtime",
                     "transition_resume", "spool_transform", "verify"):
    sys.modules.pop(_module_name, None)

from app.db.schema_upgrade.transition_api import TransitionContext  # noqa: E402
from migrate import (  # noqa: E402
    transform_dashboard,
    transform_proxy,
)
from transition_common import read_usage_spool  # noqa: E402
from verify import verify_dashboard, verify_proxy  # noqa: E402


TRANSITION_ID = "0-to-1"


def _dashboard_identity(proxy: Path) -> tuple[dict[int, str], dict[tuple[int, str], str]]:
    conn = sqlite3.connect(proxy)
    try:
        account_types = {}
        for row in conn.execute(
            "SELECT a.id, CASE WHEN i.id IS NOT NULL THEN 'agent' "
            "WHEN bc.charge_type='recurring' THEN 'plan' ELSE 'api' END AS kind "
            "FROM accounts a LEFT JOIN account_importers i ON i.account_id=a.id "
            "LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL"
        ):
            account_types[int(row[0])] = row[1]
        masks = {}
        for row in conn.execute(
            "SELECT u.account_id,c.key_masked,c.uuid "
            "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id"
        ):
            masks[(int(row[0]), row[1])] = row[2]
        return account_types, masks
    finally:
        conn.close()


def _spool_records(context: TransitionContext) -> list[dict]:
    records = context.metadata.get("spool_records")
    if records is not None:
        return list(records)
    source = context.metadata.get("spool_proxy_path")
    return read_usage_spool(Path(source)) if source else []


def apply(context: TransitionContext) -> None:
    if context.scope == "local-pair":
        spool_records = _spool_records(context)
        mapping = transform_proxy(
            context.source("token-board"), context.shadow("token-board"),
            context.source_timezone, spool_records,
        )
        transform_dashboard(
            context.source("dashboard"), context.shadow("dashboard"),
            context.source("token-board"), mapping,
            mapping.get("credential_masks"),
        )
        return

    if context.scope == "token-board-artifact":
        transform_proxy(
            context.source("token-board"), context.shadow("token-board"),
            context.source_timezone, _spool_records(context),
        )
        return

    if context.scope == "dashboard-artifact":
        account_types, masks = _dashboard_identity(context.source("token-board"))
        transform_dashboard(
            context.source("dashboard"), context.shadow("dashboard"), None,
            {"account_types": account_types, "credential_map": {}}, masks,
        )
        return

    raise RuntimeError(f"unsupported 0-to-1 transition scope: {context.scope}")


def verify(context: TransitionContext) -> dict:
    if context.scope == "local-pair":
        return {
            "token-board": verify_proxy(
                str(context.source("token-board")),
                str(context.shadow("token-board")),
                spool_records=_spool_records(context),
            ),
            "dashboard": verify_dashboard(
                str(context.source("dashboard")),
                str(context.shadow("dashboard")),
            ),
        }
    if context.scope == "token-board-artifact":
        return verify_proxy(
            str(context.source("token-board")),
            str(context.shadow("token-board")),
            spool_records=_spool_records(context),
        )
    if context.scope == "dashboard-artifact":
        return verify_dashboard(
            str(context.source("dashboard")),
            str(context.shadow("dashboard")),
        )
    raise RuntimeError(f"unsupported 0-to-1 verification scope: {context.scope}")
