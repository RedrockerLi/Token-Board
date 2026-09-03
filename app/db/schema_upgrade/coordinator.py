"""Crash-safe, unattended schema upgrade coordination."""

from __future__ import annotations

import fcntl
from pathlib import Path

from app.db.migrations import (
    DASHBOARD_DATABASE_NAME,
    TOKEN_BOARD_DATABASE_NAME,
    MigrationError,
    SchemaVersion,
    apply_sql_migrations,
)
from .compound import (
    pending_transitions,
    run as run_transitions,
)
from .artifact_runner import upgrade_artifact
from .engine_core import (
    UpgradeError,
    UpgradeResult,
    inspect_version,
    latest_version,
    recover_incomplete_manifests,
    rebuild_snapshot,
    schema_root as resolve_schema_root,
)


def verify_current_database(path: str | Path, database_name: str,
                            schema_root: str | Path | None = None) -> SchemaVersion:
    """Verify that a runtime database is already at the published V1 tip."""
    database = Path(path).resolve()
    root = resolve_schema_root(schema_root)
    current = inspect_version(database, database_name)
    if current is None:
        raise MigrationError(
            f"{database_name} database is missing or empty: {database}; "
            "run the Python schema-upgrade boundary first")
    if current.major != 1:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}; "
            "runtime accepts only current V1 databases")
    latest = latest_version(root, database_name, 1)
    if current != latest:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}, expected "
            f"V{latest.major}.{latest.minor}; run the Python schema-upgrade "
            "boundary first")
    return current


def _result(path: Path, database_name: str, previous: SchemaVersion | None,
            upgraded: bool, manifest: str | None = None) -> UpgradeResult:
    current = inspect_version(path, database_name)
    if current is None:
        raise UpgradeError(f"upgrade did not produce a versioned database: {path}")
    return UpgradeResult(str(path), database_name, previous, current,
                         upgraded, manifest)


def ensure_local_databases(proxy_path: str, dashboard_path: str,
                           schema_root: str | Path | None = None,
                           source_timezone: str = "Asia/Shanghai",
                           auto_recover: bool = True) -> dict[str, UpgradeResult]:
    """Ensure both local databases are current before services start."""
    proxy = Path(proxy_path).resolve()
    dashboard = Path(dashboard_path).resolve()
    proxy.parent.mkdir(parents=True, exist_ok=True)
    root = resolve_schema_root(schema_root)
    lock_path = proxy.parent / "schema-upgrade.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if auto_recover:
                recover_incomplete_manifests(proxy.parent)
            proxy_version = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            dashboard_version = (
                inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
                if dashboard.exists() else None
            )

            if proxy_version is None and dashboard_version is None:
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
                apply_sql_migrations(str(dashboard), str(root), DASHBOARD_DATABASE_NAME)
                return {
                    TOKEN_BOARD_DATABASE_NAME: _result(
                        proxy, TOKEN_BOARD_DATABASE_NAME, None, True),
                    DASHBOARD_DATABASE_NAME: _result(
                        dashboard, DASHBOARD_DATABASE_NAME, None, True),
                }

            if (proxy_version is not None and dashboard_version is not None and
                    proxy_version.major == 0 and dashboard_version.major == 0):
                transitions = pending_transitions(
                    proxy, dashboard, root, proxy_version, dashboard_version)
                if not transitions:
                    raise UpgradeError("V0 database pair has no matching transition route")
                result = run_transitions(
                    proxy, dashboard, root, proxy_version, dashboard_version,
                    transitions, source_timezone)
                return {
                    TOKEN_BOARD_DATABASE_NAME: result,
                    DASHBOARD_DATABASE_NAME: UpgradeResult(
                        str(dashboard), DASHBOARD_DATABASE_NAME, dashboard_version,
                        inspect_version(dashboard, DASHBOARD_DATABASE_NAME), True,
                        result.manifest),
                }

            if proxy_version is not None and proxy_version.major == 0:
                upgrade_artifact(
                    proxy, TOKEN_BOARD_DATABASE_NAME, root, source_timezone)

            dashboard_version = (
                inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
                if dashboard.exists() else None
            )
            if dashboard_version is not None and dashboard_version.major == 0:
                upgrade_artifact(
                    dashboard, DASHBOARD_DATABASE_NAME, root, source_timezone,
                    local_token_board_path=proxy)

            if not proxy.exists():
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
            if not dashboard.exists():
                apply_sql_migrations(str(dashboard), str(root), DASHBOARD_DATABASE_NAME)

            proxy_version = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            dashboard_version = inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
            if proxy_version is None or dashboard_version is None:
                raise UpgradeError("local database initialization did not produce both V1 databases")
            if proxy_version.major != 1 or dashboard_version.major != 1:
                raise UpgradeError(
                    f"unsupported local database versions: "
                    f"token-board={proxy_version}, dashboard={dashboard_version}")

            transitions = pending_transitions(
                proxy, dashboard, root, proxy_version, dashboard_version)
            if transitions:
                result = run_transitions(
                    proxy, dashboard, root, proxy_version, dashboard_version,
                    transitions, source_timezone)
            else:
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
                apply_sql_migrations(str(dashboard), str(root), DASHBOARD_DATABASE_NAME)
                result = None

            snapshot = proxy.parent / "token-board_config_snapshot.db"
            if snapshot.exists():
                snapshot_version = inspect_version(snapshot, TOKEN_BOARD_DATABASE_NAME)
                if snapshot_version is not None and snapshot_version.major == 1:
                    # Snapshots contain configuration data, so they must use
                    # the same token-board-artifact transition routes as a
                    # downloaded configuration artifact instead of bypassing
                    # data transforms through the raw SQL engine.
                    upgrade_artifact(
                        snapshot, TOKEN_BOARD_DATABASE_NAME, root,
                        source_timezone, configuration_only=True)
                elif snapshot_version is not None and snapshot_version.major == 0:
                    rebuild_snapshot(proxy)

            current_proxy = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            current_dashboard = inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
            assert current_proxy is not None and current_dashboard is not None
            return {
                TOKEN_BOARD_DATABASE_NAME: (
                    result if result is not None else UpgradeResult(
                        str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version,
                        current_proxy, current_proxy != proxy_version)),
                DASHBOARD_DATABASE_NAME: UpgradeResult(
                    str(dashboard), DASHBOARD_DATABASE_NAME, dashboard_version,
                    current_dashboard, current_dashboard != dashboard_version,
                    result.manifest if result is not None else None),
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def upgrade_shadow(path: str, database_name: str,
                   schema_root: str | Path | None = None,
                   source_timezone: str = "Asia/Shanghai",
                   local_token_board_path: str | None = None,
                   configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a local or downloaded artifact through the checked shadow path."""
    root = resolve_schema_root(schema_root)
    return upgrade_artifact(
        Path(path).resolve(), database_name, root, source_timezone,
        Path(local_token_board_path).resolve()
        if local_token_board_path else None,
        configuration_only,
    )


def upgrade_downloaded_artifact(path: str, database_name: str,
                                schema_root: str | Path | None = None, *,
                                local_token_board_path: str | None = None,
                                source_timezone: str = "Asia/Shanghai",
                                configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a downloaded artifact through the same shadow runner."""
    return upgrade_shadow(
        path, database_name, schema_root, source_timezone,
        local_token_board_path, configuration_only)
