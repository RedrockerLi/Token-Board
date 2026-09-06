"""Crash-safe, unattended schema upgrade coordination."""

from __future__ import annotations

import fcntl
import shutil
import uuid
from pathlib import Path

from app.core import sqlite_runtime
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
    replace,
    verify,
    backup_files,
    now,
    write_manifest,
    schema_root as resolve_schema_root,
)
from .sqlite_utils import copy_sqlite


def verify_current_database(path: str | Path, database_name: str,
                            schema_root: str | Path | None = None) -> SchemaVersion:
    """Verify that a runtime database is already at the published V2 tip."""
    database = Path(path).resolve()
    root = resolve_schema_root(schema_root)
    current = inspect_version(database, database_name)
    if current is None:
        raise MigrationError(
            f"{database_name} database is missing or empty: {database}; "
            "run the Python schema-upgrade boundary first")
    latest_major = max(
        int(path.name[1:]) for path in root.joinpath(database_name).glob("v*")
        if path.is_dir() and path.name[1:].isdigit()
    )
    if current.major != latest_major:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}; "
            f"runtime accepts only current V{latest_major} databases")
    latest = latest_version(root, database_name, latest_major)
    if current != latest:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}, expected "
            f"V{latest.major}.{latest.minor}; run the Python schema-upgrade "
            "boundary first")
    return current


def _latest_major(root: Path, database_name: str) -> int:
    majors = [int(path.name[1:]) for path in root.joinpath(database_name).glob("v*")
              if path.is_dir() and path.name[1:].isdigit()]
    if not majors:
        raise UpgradeError(f"no schema major for {database_name}")
    return max(majors)


def _upgrade_v2_pair(proxy: Path, dashboard: Path, root: Path,
                     proxy_version: SchemaVersion,
                     dashboard_version: SchemaVersion) -> dict[str, UpgradeResult]:
    """Upgrade a V1/V2 pair through one checked shadow barrier.

    One side can already be V2 when a downloaded V0 artifact was converted
    first. It is still copied and verified in the same barrier so no mixed
    pair is ever published as a runnable local state.
    """
    if (proxy_version.major not in (1, 2) or
            dashboard_version.major not in (1, 2) or
            proxy_version.major == dashboard_version.major == 2):
        raise UpgradeError("V2 shadow upgrade requires at least one V1 database")
    work = proxy.parent / f"auto-v1-to-v2-{uuid.uuid4().hex}.work"
    backup = proxy.parent / f"auto-v1-to-v2-{uuid.uuid4().hex}.backup"
    manifest_path = proxy.parent / f"auto-v1-to-v2-{uuid.uuid4().hex}.manifest.json"
    work.mkdir(mode=0o700, exist_ok=False)
    try:
        manifest = {
            "kind": "v1-to-v2",
            "stage": "prepared",
            "work_dir": str(work),
            "backup_dir": str(backup),
            "sources": {"token-board": str(proxy), "dashboard": str(dashboard)},
        }
        write_manifest(manifest_path, manifest)
        manifest["backups"] = backup_files([proxy, dashboard], backup)
        manifest["stage"] = "backed_up"
        write_manifest(manifest_path, manifest)
        proxy_shadow = work / "token-board.v2-shadow.db"
        dashboard_shadow = work / "dashboard.v2-shadow.db"
        copy_sqlite(proxy, proxy_shadow)
        copy_sqlite(dashboard, dashboard_shadow)
        proxy_target = latest_version(root, TOKEN_BOARD_DATABASE_NAME, 2)
        dashboard_target = latest_version(root, DASHBOARD_DATABASE_NAME, 2)
        apply_sql_migrations(str(proxy_shadow), str(root),
                             TOKEN_BOARD_DATABASE_NAME, target=proxy_target)
        apply_sql_migrations(str(dashboard_shadow), str(root),
                             DASHBOARD_DATABASE_NAME, target=dashboard_target)
        verify(proxy_shadow, TOKEN_BOARD_DATABASE_NAME, proxy_target)
        verify(dashboard_shadow, DASHBOARD_DATABASE_NAME, dashboard_target)
        manifest["shadows"] = {"token-board": str(proxy_shadow),
                                "dashboard": str(dashboard_shadow)}
        manifest["stage"] = "verified"
        write_manifest(manifest_path, manifest)
        replace(proxy, proxy_shadow)
        manifest["stage"] = "token_board_replaced"
        write_manifest(manifest_path, manifest)
        replace(dashboard, dashboard_shadow)
        manifest["stage"] = "dashboard_replaced"
        write_manifest(manifest_path, manifest)
        rebuild_snapshot(proxy)
        manifest["stage"] = "complete"
        write_manifest(manifest_path, manifest)
        return {
            TOKEN_BOARD_DATABASE_NAME: _result(
                proxy, TOKEN_BOARD_DATABASE_NAME, proxy_version, True,
                str(manifest_path)),
            DASHBOARD_DATABASE_NAME: _result(
                dashboard, DASHBOARD_DATABASE_NAME, dashboard_version, True,
                str(manifest_path)),
        }
    except Exception:
        # The caller still owns the original files; backups are retained for
        # operator inspection and recovery rather than silently discarded.
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
                proxy_version = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
                dashboard_version = inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
                if proxy_version is None or dashboard_version is None:
                    raise UpgradeError("fresh database initialization did not produce both databases")
                if (_latest_major(root, TOKEN_BOARD_DATABASE_NAME) >= 2 and
                        proxy_version.major == dashboard_version.major == 1):
                    return _upgrade_v2_pair(
                        proxy, dashboard, root, proxy_version, dashboard_version)
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
                transitioned_proxy = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
                transitioned_dashboard = inspect_version(dashboard, DASHBOARD_DATABASE_NAME)
                if (_latest_major(root, TOKEN_BOARD_DATABASE_NAME) >= 2 and
                        transitioned_proxy is not None and
                        transitioned_dashboard is not None and
                        transitioned_proxy.major == transitioned_dashboard.major == 1):
                    return _upgrade_v2_pair(
                        proxy, dashboard, root,
                        transitioned_proxy, transitioned_dashboard)
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
                raise UpgradeError("local database initialization did not produce both V2 databases")
            if ({proxy_version.major, dashboard_version.major} == {1, 2}):
                state_conn = sqlite_runtime.connect(proxy, "schema_upgrade")
                try:
                    pending = state_conn.execute(
                        "SELECT 1 FROM sync_state WHERE key LIKE 'dashboard_pending_%' "
                        "AND value<>'' LIMIT 1").fetchone()
                finally:
                    state_conn.close()
                if pending:
                    raise UpgradeError(
                        "Dashboard has a pending V1 transaction; recover it "
                        "before starting the V2 upgrade")
                return _upgrade_v2_pair(
                    proxy, dashboard, root, proxy_version, dashboard_version)
            if proxy_version.major != dashboard_version.major:
                raise UpgradeError(
                    f"unsupported local database versions: "
                    f"token-board={proxy_version}, dashboard={dashboard_version}")

            # A pending Dashboard export is a V1 transaction boundary.  It
            # must be recovered by the sync protocol before the pair is
            # upgraded; guessing here could lose a high-water mark or publish
            # an incomplete archive.
            if proxy_version.major == 1:
                state_conn = sqlite_runtime.connect(proxy, "schema_upgrade")
                try:
                    pending = state_conn.execute(
                        "SELECT 1 FROM sync_state WHERE key LIKE 'dashboard_pending_%' "
                        "AND value<>'' LIMIT 1").fetchone()
                finally:
                    state_conn.close()
                if pending:
                    raise UpgradeError(
                        "Dashboard has a pending V1 transaction; recover it "
                        "before starting the V2 upgrade")

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
            if (_latest_major(root, TOKEN_BOARD_DATABASE_NAME) >= 2 and
                    current_proxy.major == 1 and current_dashboard.major == 1):
                return _upgrade_v2_pair(
                    proxy, dashboard, root, current_proxy, current_dashboard)
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
