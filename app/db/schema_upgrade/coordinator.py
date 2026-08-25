"""Crash-safe, unattended schema upgrade coordination."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core import sqlite_runtime
from app.db.migrations import (
    TOKEN_BOARD_DATABASE_NAME,
    MigrationError,
    SchemaVersion,
    apply_sql_migrations,
    migrate,
)
from .artifact import strip_runtime_artifact
from .compound import apply_artifact_pair, pending_transitions, run as run_compound
from .engine_core import (
    UpgradeError,
    UpgradeResult,
    backup_files as _backup_files,
    inspect_version,
    latest_version as _latest_version,
    now as _now,
    repo_root as _repo_root,
    recover_incomplete_manifests as _recover_incomplete_manifests,
    rebuild_snapshot as _rebuild_snapshot,
    replace as _replace,
    schema_root as _schema_root,
    table_exists as _table_exists,
    verify as _verify,
    write_manifest as _write_manifest,
)
from .sqlite_utils import copy_sqlite as _copy_sqlite

def verify_current_database(path: str | Path, database_name: str,
                            schema_root: str | Path | None = None) -> SchemaVersion:
    """Verify that a runtime database is already at the published V1 tip.

    This is intentionally read-only.  Runtime façades use it as a guard after
    the Python startup boundary; they never create metadata or apply SQL.
    """
    database = Path(path).resolve()
    root = _schema_root(schema_root)
    current = inspect_version(database, database_name)
    if current is None:
        raise MigrationError(
            f"{database_name} database is missing or empty: {database}; "
            "run the Python schema-upgrade boundary first")
    if current.major != 1:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}; "
            "runtime accepts only current V1 databases")
    latest = _latest_version(root, database_name, 1)
    if current != latest:
        raise MigrationError(
            f"{database} is V{current.major}.{current.minor}, expected "
            f"V{latest.major}.{latest.minor}; run the Python schema-upgrade "
            "boundary first")
    return current


def _transition_module(name: str):
    directory = _repo_root() / "schema" / "transitions" / "0-to-1"
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    path = directory / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"token_board_transition_{name}", path)
    if spec is None or spec.loader is None:
        raise UpgradeError(f"cannot load transition module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
def _stage_v0(source: Path, database_name: str, schema_root: Path,
              work_dir: Path, source_version: SchemaVersion) -> Path:
    staged = work_dir / f"{database_name}.v0-staged.db"
    _copy_sqlite(source, staged)
    latest_v0 = _latest_version(schema_root, database_name, 0)
    if source_version.minor < latest_v0.minor:
        migrate(str(staged), str(schema_root / database_name / "v0"), database_name)
    return staged
def _v1_dashboard_identity(proxy_path: Path) -> tuple[dict, dict]:
    conn = sqlite_runtime.connect(proxy_path, "schema_upgrade")
    conn.row_factory = sqlite3.Row
    try:
        account_types = {}
        for row in conn.execute(
            "SELECT a.id, CASE WHEN i.id IS NOT NULL THEN 'agent' "
            "WHEN bc.charge_type='recurring' THEN 'plan' ELSE 'api' END AS kind "
            "FROM accounts a LEFT JOIN account_importers i ON i.account_id=a.id "
            "LEFT JOIN billing_contracts bc ON bc.account_id=a.id AND bc.valid_until IS NULL"
        ):
            account_types[int(row["id"])] = row["kind"]
        masks = {}
        for row in conn.execute(
            "SELECT u.account_id,c.key_masked,c.uuid "
            "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id"
        ):
            masks[(int(row["account_id"]), row["key_masked"])] = row["uuid"]
        return account_types, masks
    finally:
        conn.close()


def _transition_pair_impl(proxy: Path, dashboard: Path, schema_root: Path,
                          timezone_name: str) -> UpgradeResult:
    transition = _transition_module("migrate")
    source_tz = ZoneInfo(timezone_name)
    stamp = _now()
    manifest_path = proxy.parent / f"auto-v0-to-v1-{stamp}.manifest.json"
    work_dir = proxy.parent / f"auto-v0-to-v1-{stamp}.work"
    backup_dir = proxy.parent / f"auto-v0-to-v1-{stamp}.backup"
    work_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "transition": "0-to-current-v1", "stage": "started",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "timezone": timezone_name, "token_board_db": str(proxy),
        "dashboard_db": str(dashboard), "schema_dir": str(schema_root),
        "work_dir": str(work_dir), "backup_dir": str(backup_dir),
    }
    _write_manifest(manifest_path, manifest)
    backup_paths = [proxy, dashboard]
    for base in (proxy, dashboard):
        backup_paths.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm"))
    backup_paths.extend((proxy.parent / name) for name in (
        "sync_config.json", "config_snapshot.json", "token-board_config_snapshot.db",
        "token-board_config_snapshot.db-wal", "token-board_config_snapshot.db-shm"))
    backup_paths.append(Path(str(proxy) + ".request-log.spool"))
    manifest["backups"] = _backup_files(backup_paths, backup_dir)
    manifest["stage"] = "backed_up"
    _write_manifest(manifest_path, manifest)

    proxy_version = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
    dashboard_version = inspect_version(dashboard, "dashboard")
    if proxy_version is None or proxy_version.major != 0:
        raise UpgradeError("paired transition requires a V0 proxy database")
    if dashboard_version is None or dashboard_version.major != 0:
        raise UpgradeError("paired transition requires a V0 dashboard database")

    proxy_source = _stage_v0(proxy, TOKEN_BOARD_DATABASE_NAME, schema_root, work_dir, proxy_version)
    dashboard_source = _stage_v0(dashboard, "dashboard", schema_root, work_dir, dashboard_version)
    spool_records = transition.read_usage_spool(proxy)
    manifest["spool"] = {
        "path": str(proxy) + ".request-log.spool",
        "records": len(spool_records),
    }
    _write_manifest(manifest_path, manifest)
    proxy_shadow = work_dir / "token-board.v1-shadow.db"
    dashboard_shadow = work_dir / "dashboard.v1-shadow.db"
    migrate(str(proxy_shadow), str(schema_root), TOKEN_BOARD_DATABASE_NAME)
    migrate(str(dashboard_shadow), str(schema_root), "dashboard")
    manifest["stage"] = "shadows_created"
    _write_manifest(manifest_path, manifest)

    mapping = transition.transform_proxy(proxy_source, proxy_shadow, source_tz,
                                         spool_records)
    transition.transform_dashboard(dashboard_source, dashboard_shadow,
                                    proxy_source, mapping,
                                    mapping.get("credential_masks"))
    manifest["stage"] = "transformed"
    _write_manifest(manifest_path, manifest)
    _verify(proxy_shadow, TOKEN_BOARD_DATABASE_NAME, _latest_version(schema_root, TOKEN_BOARD_DATABASE_NAME, 1))
    _verify(dashboard_shadow, "dashboard", _latest_version(schema_root, "dashboard", 1))
    manifest["stage"] = "verified"
    _write_manifest(manifest_path, manifest)

    _replace(proxy, proxy_shadow)
    manifest["stage"] = "token_board_replaced"
    _write_manifest(manifest_path, manifest)
    _replace(dashboard, dashboard_shadow)
    manifest["stage"] = "dashboard_replaced"
    _write_manifest(manifest_path, manifest)
    _rebuild_snapshot(proxy)
    manifest["stage"] = "snapshot_rebuilt"
    _write_manifest(manifest_path, manifest)
    Path(str(proxy) + ".request-log.spool").unlink(missing_ok=True)
    manifest["stage"] = "complete"
    _write_manifest(manifest_path, manifest)
    shutil.rmtree(work_dir, ignore_errors=True)
    current = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
    assert current is not None
    return UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version, current, True,
                         str(manifest_path))


def _transition_pair(proxy: Path, dashboard: Path, schema_root: Path,
                     timezone_name: str) -> UpgradeResult:
    """Run paired transition; restore all backups on ordinary failure."""
    try:
        return _transition_pair_impl(proxy, dashboard, schema_root, timezone_name)
    except Exception:
        try:
            _recover_incomplete_manifests(proxy.parent)
        except Exception as recovery_error:
            raise UpgradeError(f"transition rollback failed: {recovery_error}") \
                from recovery_error
        raise


def ensure_local_databases(proxy_path: str, dashboard_path: str,
                           schema_root: str | Path | None = None,
                           source_timezone: str = "Asia/Shanghai",
                           auto_recover: bool = True) -> dict[str, UpgradeResult]:
    """Ensure both local databases are current before services start."""
    proxy = Path(proxy_path).resolve()
    dashboard = Path(dashboard_path).resolve()
    proxy.parent.mkdir(parents=True, exist_ok=True)
    root = _schema_root(schema_root)
    lock_path = proxy.parent / "schema-upgrade.lock"
    with lock_path.open("a+b") as lock:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if auto_recover:
                _recover_incomplete_manifests(proxy.parent)
            pv = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            dv = inspect_version(dashboard, "dashboard") if dashboard.exists() else None
            if pv is None and dv is None:
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
                apply_sql_migrations(str(dashboard), str(root), "dashboard")
                return {
                    TOKEN_BOARD_DATABASE_NAME: UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, None,
                                             inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME), True),
                    "dashboard": UpgradeResult(str(dashboard), "dashboard", None,
                                                 inspect_version(dashboard, "dashboard"), True),
                }
            if pv is not None and pv.major == 0 and dv is not None and dv.major == 0:
                result = _transition_pair(proxy, dashboard, root, source_timezone)
                return {TOKEN_BOARD_DATABASE_NAME: result, "dashboard": UpgradeResult(
                    str(dashboard), "dashboard", dv,
                    inspect_version(dashboard, "dashboard"), True,
                    result.manifest)}
            if pv is not None and pv.major == 0:
                backup = proxy.with_name(proxy.name + ".v0-local-backup")
                if not backup.exists():
                    shutil.copy2(proxy, backup)
                result = upgrade_shadow(str(proxy), TOKEN_BOARD_DATABASE_NAME, root,
                                        source_timezone)
                _rebuild_snapshot(proxy)
                pv = result.current
            if dv is not None and dv.major == 0:
                backup = dashboard.with_name(dashboard.name + ".v0-local-backup")
                if not backup.exists():
                    shutil.copy2(dashboard, backup)
                work = dashboard.parent / f".{dashboard.name}.local-v1"
                work.mkdir(mode=0o700, exist_ok=True)
                try:
                    shadow = work / "dashboard-v1.db"
                    migrate(str(shadow), str(root), "dashboard")
                    transition = _transition_module("migrate")
                    source_tz = ZoneInfo(source_timezone)
                    if proxy.exists() and inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME) is not None:
                        types, masks = _v1_dashboard_identity(proxy)
                    else:
                        types, masks = {}, {}
                    transition.transform_dashboard(
                        dashboard, shadow, None,
                        {"account_types": types, "credential_map": {}},
                        masks)
                    _verify(shadow, "dashboard", _latest_version(root, "dashboard", 1))
                    _replace(dashboard, shadow)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                dv = inspect_version(dashboard, "dashboard")
            if pv is not None and pv.major != 1:
                raise UpgradeError(f"unsupported proxy schema V{pv.major}.{pv.minor}")
            if dv is not None and dv.major != 1:
                raise UpgradeError(f"unsupported dashboard schema V{dv.major}.{dv.minor}")
            if not proxy.exists():
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
            if not dashboard.exists():
                apply_sql_migrations(str(dashboard), str(root), "dashboard")
            pv = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            dv = inspect_version(dashboard, "dashboard")
            if pv is None or dv is None:
                raise UpgradeError("local database initialization did not produce both V1 databases")
            pending = pending_transitions(proxy, dashboard, root, pv, dv)
            if pending:
                result = run_compound(proxy, dashboard, root, pv, dv, pending)
            else:
                apply_sql_migrations(str(proxy), str(root), TOKEN_BOARD_DATABASE_NAME)
                apply_sql_migrations(str(dashboard), str(root), "dashboard")
                result = None
            snapshot = proxy.parent / "token-board_config_snapshot.db"
            if snapshot.exists():
                snapshot_version = inspect_version(snapshot, TOKEN_BOARD_DATABASE_NAME)
                if snapshot_version is not None and snapshot_version.major == 1:
                    apply_sql_migrations(str(snapshot), str(root), TOKEN_BOARD_DATABASE_NAME)
                elif snapshot_version is not None and snapshot_version.major == 0:
                    _rebuild_snapshot(proxy)
            current_proxy = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
            current_dashboard = inspect_version(dashboard, "dashboard")
            assert current_proxy is not None and current_dashboard is not None
            return {
                TOKEN_BOARD_DATABASE_NAME: (result if result is not None else UpgradeResult(
                    str(proxy), TOKEN_BOARD_DATABASE_NAME, pv, current_proxy,
                    current_proxy != pv)),
                "dashboard": UpgradeResult(
                    str(dashboard), "dashboard", dv, current_dashboard,
                    current_dashboard != dv,
                    result.manifest if result is not None else None),
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def upgrade_shadow(path: str, database_name: str,
                   schema_root: str | Path | None = None,
                   source_timezone: str = "Asia/Shanghai",
                   local_token_board_path: str | None = None,
                   configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a downloaded artifact in place, never touching its source."""
    database = Path(path).resolve()
    root = _schema_root(schema_root)
    current = inspect_version(database, database_name)
    if current is None:
        apply_sql_migrations(str(database), str(root), database_name)
        if configuration_only:
            strip_runtime_artifact(database, database_name)
        latest = inspect_version(database, database_name)
        assert latest is not None
        return UpgradeResult(str(database), database_name, None, latest, True)
    if current.major == 1:
        work = database.parent / f".{database.name}.v1-upgrade-{uuid.uuid4().hex}"
        work.mkdir(mode=0o700)
        try:
            artifact_shadow = work / f"{database_name}.v1.db"
            _copy_sqlite(database, artifact_shadow)
            if database_name == "dashboard" and local_token_board_path:
                local_proxy = Path(local_token_board_path).resolve()
                local_version = inspect_version(local_proxy, TOKEN_BOARD_DATABASE_NAME)
                if local_version is None or local_version.major != 1:
                    raise UpgradeError(
                        "dashboard V1 upgrade requires a current V1 proxy")
                proxy_shadow = work / "local-token-board.v1-shadow.db"
                _copy_sqlite(local_proxy, proxy_shadow)
                pending = pending_transitions(
                    proxy_shadow, artifact_shadow, root, local_version, current)
                if pending:
                    apply_artifact_pair(
                        proxy_shadow, artifact_shadow, root, pending)
                else:
                    apply_sql_migrations(
                        str(artifact_shadow), str(root), "dashboard")
            else:
                apply_sql_migrations(
                    str(artifact_shadow), str(root), database_name)
            expected = _latest_version(root, database_name, 1)
            _verify(artifact_shadow, database_name, expected)
            if configuration_only:
                strip_runtime_artifact(artifact_shadow, database_name)
            _replace(database, artifact_shadow)
            latest = inspect_version(database, database_name)
            assert latest is not None
            return UpgradeResult(str(database), database_name, current, latest,
                                 latest != current)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if current.major != 0:
        raise UpgradeError(f"unsupported remote {database_name} V{current.major}.{current.minor}")
    work = database.parent / f".{database.name}.upgrade-{uuid.uuid4().hex}"
    work.mkdir(mode=0o700)
    try:
        staged = _stage_v0(database, database_name, root, work, current)
        shadow = work / f"{database_name}.v1.db"
        migrate(str(shadow), str(root), database_name)
        transition = _transition_module("migrate")
        source_tz = ZoneInfo(source_timezone)
        if database_name == TOKEN_BOARD_DATABASE_NAME:
            spool_records = transition.read_usage_spool(database)
            transition.transform_proxy(staged, shadow, source_tz, spool_records)
        else:
            if not local_token_board_path:
                raise UpgradeError("dashboard V0 upgrade requires local proxy identity")
            local_proxy = Path(local_token_board_path)
            local_version = inspect_version(local_proxy, TOKEN_BOARD_DATABASE_NAME)
            if local_version is None or local_version.major != 1:
                raise UpgradeError("dashboard V0 upgrade requires a current V1 proxy")
            types, masks = _v1_dashboard_identity(local_proxy)
            transition.transform_dashboard(
                staged, shadow, None,
                {"account_types": types, "credential_map": {}}, masks)
        _verify(shadow, database_name, _latest_version(root, database_name, 1))
        if configuration_only:
            strip_runtime_artifact(shadow, database_name)
        _replace(database, shadow)
        if database_name == TOKEN_BOARD_DATABASE_NAME:
            Path(str(database) + ".request-log.spool").unlink(missing_ok=True)
        return UpgradeResult(str(database), database_name, current,
                             inspect_version(database, database_name), True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def upgrade_downloaded_artifact(path: str, database_name: str,
                                schema_root: str | Path | None = None, *,
                                local_token_board_path: str | None = None,
                                source_timezone: str = "Asia/Shanghai",
                                configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a downloaded artifact through the checked shadow path."""
    return upgrade_shadow(path, database_name, schema_root, source_timezone,
                          local_token_board_path, configuration_only)
