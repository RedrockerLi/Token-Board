"""Crash-safe, unattended schema upgrade coordination."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.migrations import MigrationError, SchemaVersion, migrate
from .artifact import strip_runtime_artifact


class UpgradeError(RuntimeError):
    """Raised when a database cannot be upgraded safely."""


@dataclass(frozen=True)
class UpgradeResult:
    path: str
    database_name: str
    previous: SchemaVersion | None
    current: SchemaVersion
    upgraded: bool
    manifest: str | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _schema_root(schema_root: str | Path | None) -> Path:
    return Path(schema_root or (_repo_root() / "schema")).resolve()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def inspect_version(path: Path, database_name: str) -> SchemaVersion | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    conn = sqlite3.connect(path)
    try:
        row = None
        if _table_exists(conn, "schema_version"):
            row = conn.execute(
                "SELECT major,minor,database_name FROM schema_version WHERE id=1"
            ).fetchone()
        pragma = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if row:
            if row[2] != database_name:
                raise UpgradeError(
                    f"{path} is {row[2]!r}, expected {database_name!r}")
            expected = int(row[0]) * 10_000 + int(row[1])
            if pragma != expected:
                raise UpgradeError(f"{path}: schema metadata disagrees with PRAGMA")
            return SchemaVersion(int(row[0]), int(row[1]))
        if pragma:
            return SchemaVersion.from_user_version(pragma)
        business = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
        if business:
            raise UpgradeError(f"{path}: non-empty database has no schema version")
        return None
    finally:
        conn.close()


def _latest_version(schema_root: Path, database_name: str, major: int) -> SchemaVersion:
    directory = schema_root / database_name / f"v{major}"
    versions = []
    for path in directory.glob("*.sql"):
        stem = path.name.split("_", 1)[0]
        try:
            m, n = (int(value) for value in stem.split("-", 1))
        except ValueError:
            continue
        versions.append(SchemaVersion(m, n))
    if not versions:
        raise UpgradeError(f"no schema files for {database_name} V{major}")
    return max(versions)


def _copy_sqlite(source: Path, destination: Path) -> None:
    destination.unlink(missing_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.execute("PRAGMA busy_timeout=5000")
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


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
def _write_manifest(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
def _backup_files(paths: list[Path], backup_dir: Path) -> list[dict]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    result = []
    for source in paths:
        item = {"source": str(source), "existed": source.is_file()}
        if source.is_file():
            target = backup_dir / source.name
            shutil.copy2(source, target)
            item.update({"backup": str(target), "sha256": _checksum(target),
                         "size": target.stat().st_size})
        result.append(item)
    return result
def _recover_incomplete_manifests(root: Path) -> None:
    for manifest_path in sorted(root.glob("auto-v0-to-v1-*.manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest.get("stage") in {"complete", "recovered_rollback"}:
            continue
        backups = manifest.get("backups") or []
        if not backups:
            # No source file was replaced; just clean stale shadows.
            shutil.rmtree(manifest.get("work_dir", ""), ignore_errors=True)
            manifest["stage"] = "recovered_rollback"
            _write_manifest(manifest_path, manifest)
            continue
        for item in backups:
            source = Path(item["source"])
            backup = item.get("backup")
            if item.get("existed"):
                if not backup or not Path(backup).is_file():
                    raise UpgradeError(f"incomplete transition backup missing: {source}")
                if item.get("sha256") and _checksum(Path(backup)) != item["sha256"]:
                    raise UpgradeError(f"incomplete transition backup checksum mismatch: {backup}")
                shutil.copy2(backup, source)
            else:
                source.unlink(missing_ok=True)
        shutil.rmtree(manifest.get("work_dir", ""), ignore_errors=True)
        manifest["stage"] = "recovered_rollback"
        _write_manifest(manifest_path, manifest)
def _verify(path: Path, database_name: str, expected: SchemaVersion) -> None:
    conn = sqlite3.connect(path)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise UpgradeError(f"{path}: quick_check failed: {quick}")
        foreign = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign:
            raise UpgradeError(f"{path}: foreign_key_check failed: {tuple(foreign)}")
    finally:
        conn.close()
    actual = inspect_version(path, database_name)
    if actual != expected:
        raise UpgradeError(f"{path}: expected V{expected.major}.{expected.minor}, got {actual}")
def _replace(source: Path, shadow: Path) -> None:
    conn = sqlite3.connect(shadow, isolation_level=None)
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise UpgradeError(f"shadow WAL is busy: {shadow}")
    finally:
        conn.close()
    os.replace(shadow, source)
    Path(str(source) + "-wal").unlink(missing_ok=True)
    Path(str(source) + "-shm").unlink(missing_ok=True)
def _v1_dashboard_identity(proxy_path: Path) -> tuple[dict, dict]:
    conn = sqlite3.connect(proxy_path)
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


def _rebuild_snapshot(proxy_path: Path) -> None:
    snapshot = proxy_path.parent / "config_snapshot.db"
    temporary = snapshot.with_name("config_snapshot.db.upgrade-new")
    temporary.unlink(missing_ok=True)
    _copy_sqlite(proxy_path, temporary)
    conn = sqlite3.connect(temporary)
    try:
        for table in ("request_attempts", "request_log", "billing_period_charges", "fx_rates"):
            if _table_exists(conn, table):
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    os.replace(temporary, snapshot)


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
        "timezone": timezone_name, "proxy_db": str(proxy),
        "dashboard_db": str(dashboard), "schema_dir": str(schema_root),
        "work_dir": str(work_dir), "backup_dir": str(backup_dir),
    }
    _write_manifest(manifest_path, manifest)
    backup_paths = [proxy, dashboard]
    for base in (proxy, dashboard):
        backup_paths.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm"))
    backup_paths.extend((proxy.parent / name) for name in (
        "sync_config.json", "config_snapshot.json", "config_snapshot.db",
        "config_snapshot.db-wal", "config_snapshot.db-shm"))
    backup_paths.append(Path(str(proxy) + ".request-log.spool"))
    manifest["backups"] = _backup_files(backup_paths, backup_dir)
    manifest["stage"] = "backed_up"
    _write_manifest(manifest_path, manifest)

    proxy_version = inspect_version(proxy, "proxy")
    dashboard_version = inspect_version(dashboard, "dashboard")
    if proxy_version is None or proxy_version.major != 0:
        raise UpgradeError("paired transition requires a V0 proxy database")
    if dashboard_version is None or dashboard_version.major != 0:
        raise UpgradeError("paired transition requires a V0 dashboard database")

    proxy_source = _stage_v0(proxy, "proxy", schema_root, work_dir, proxy_version)
    dashboard_source = _stage_v0(dashboard, "dashboard", schema_root, work_dir, dashboard_version)
    spool_records = transition.read_usage_spool(proxy)
    manifest["spool"] = {
        "path": str(proxy) + ".request-log.spool",
        "records": len(spool_records),
    }
    _write_manifest(manifest_path, manifest)
    proxy_shadow = work_dir / "proxy.v1-shadow.db"
    dashboard_shadow = work_dir / "dashboard.v1-shadow.db"
    migrate(str(proxy_shadow), str(schema_root), "proxy")
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
    _verify(proxy_shadow, "proxy", _latest_version(schema_root, "proxy", 1))
    _verify(dashboard_shadow, "dashboard", _latest_version(schema_root, "dashboard", 1))
    manifest["stage"] = "verified"
    _write_manifest(manifest_path, manifest)

    _replace(proxy, proxy_shadow)
    manifest["stage"] = "proxy_replaced"
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
    current = inspect_version(proxy, "proxy")
    assert current is not None
    return UpgradeResult(str(proxy), "proxy", proxy_version, current, True,
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
            pv = inspect_version(proxy, "proxy")
            dv = inspect_version(dashboard, "dashboard") if dashboard.exists() else None
            if pv is None and dv is None:
                migrate(str(proxy), str(root), "proxy")
                migrate(str(dashboard), str(root), "dashboard")
                return {
                    "proxy": UpgradeResult(str(proxy), "proxy", None,
                                             inspect_version(proxy, "proxy"), True),
                    "dashboard": UpgradeResult(str(dashboard), "dashboard", None,
                                                 inspect_version(dashboard, "dashboard"), True),
                }
            if pv is not None and pv.major == 0 and dv is not None and dv.major == 0:
                result = _transition_pair(proxy, dashboard, root, source_timezone)
                return {"proxy": result, "dashboard": UpgradeResult(
                    str(dashboard), "dashboard", dv,
                    inspect_version(dashboard, "dashboard"), True,
                    result.manifest)}
            if pv is not None and pv.major == 0:
                backup = proxy.with_name(proxy.name + ".v0-local-backup")
                if not backup.exists():
                    shutil.copy2(proxy, backup)
                result = upgrade_shadow(str(proxy), "proxy", root,
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
                    if proxy.exists() and inspect_version(proxy, "proxy") is not None:
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
                migrate(str(proxy), str(root), "proxy")
            if not dashboard.exists():
                migrate(str(dashboard), str(root), "dashboard")
            migrate(str(proxy), str(root), "proxy")
            migrate(str(dashboard), str(root), "dashboard")
            return {
                "proxy": UpgradeResult(str(proxy), "proxy", pv,
                                         inspect_version(proxy, "proxy"), pv is not None),
                "dashboard": UpgradeResult(str(dashboard), "dashboard", dv,
                                             inspect_version(dashboard, "dashboard"), dv is not None),
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def upgrade_shadow(path: str, database_name: str,
                   schema_root: str | Path | None = None,
                   source_timezone: str = "Asia/Shanghai",
                   local_proxy_path: str | None = None,
                   configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a downloaded artifact in place, never touching its source."""
    database = Path(path).resolve()
    root = _schema_root(schema_root)
    current = inspect_version(database, database_name)
    if current is None:
        migrate(str(database), str(root), database_name)
        if configuration_only:
            strip_runtime_artifact(database, database_name)
        latest = inspect_version(database, database_name)
        assert latest is not None
        return UpgradeResult(str(database), database_name, None, latest, True)
    if current.major == 1:
        migrate(str(database), str(root), database_name)
        if configuration_only:
            strip_runtime_artifact(database, database_name)
        latest = inspect_version(database, database_name)
        assert latest is not None
        return UpgradeResult(str(database), database_name, current, latest,
                             latest != current)
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
        if database_name == "proxy":
            spool_records = transition.read_usage_spool(database)
            transition.transform_proxy(staged, shadow, source_tz, spool_records)
        else:
            if not local_proxy_path:
                raise UpgradeError("dashboard V0 upgrade requires local proxy identity")
            local_proxy = Path(local_proxy_path)
            local_version = inspect_version(local_proxy, "proxy")
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
        if database_name == "proxy":
            Path(str(database) + ".request-log.spool").unlink(missing_ok=True)
        return UpgradeResult(str(database), database_name, current,
                             inspect_version(database, database_name), True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def upgrade_downloaded_artifact(path: str, database_name: str,
                                schema_root: str | Path | None = None, *,
                                local_proxy_path: str | None = None,
                                source_timezone: str = "Asia/Shanghai",
                                configuration_only: bool = False) -> UpgradeResult:
    """Upgrade a downloaded artifact through the checked shadow path."""
    return upgrade_shadow(path, database_name, schema_root, source_timezone,
                          local_proxy_path, configuration_only)
