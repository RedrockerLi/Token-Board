"""Shared, version-neutral primitives for the Python schema engine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core import sqlite_runtime
from app.db.migrations import SchemaVersion
from .sqlite_utils import copy_sqlite


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


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def schema_root(schema_dir: str | Path | None) -> Path:
    return Path(schema_dir or (repo_root() / "schema")).resolve()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def inspect_version(path: Path, database_name: str) -> SchemaVersion | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    conn = sqlite_runtime.connect(path, "schema_upgrade")
    try:
        row = None
        if table_exists(conn, "schema_version"):
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
            "AND name NOT LIKE 'sqlite_%' AND name NOT IN "
            "('schema_version','schema_migrations','schema_transitions')"
        ).fetchone()[0]
        if business:
            raise UpgradeError(f"{path}: non-empty database has no schema version")
        return None
    finally:
        conn.close()


def latest_version(schema_dir: Path, database_name: str,
                   major: int) -> SchemaVersion:
    directory = schema_dir / database_name / f"v{major}"
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


def write_manifest(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def backup_files(paths: list[Path], backup_dir: Path) -> list[dict]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    result = []
    for source in paths:
        item = {"source": str(source), "existed": source.is_file()}
        if source.is_file():
            target = backup_dir / source.name
            shutil.copy2(source, target)
            item.update({"backup": str(target), "sha256": checksum(target),
                         "size": target.stat().st_size})
        result.append(item)
    return result


def recover_incomplete_manifests(root: Path) -> None:
    for manifest_path in sorted(root.glob("auto-*.manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "work_dir" not in manifest or "backups" not in manifest:
            continue
        if manifest.get("stage") in {"complete", "recovered_rollback"}:
            continue
        backups = manifest.get("backups") or []
        if not backups:
            shutil.rmtree(manifest.get("work_dir", ""), ignore_errors=True)
            manifest["stage"] = "recovered_rollback"
            write_manifest(manifest_path, manifest)
            continue
        for item in backups:
            source = Path(item["source"])
            backup = item.get("backup")
            if item.get("existed"):
                if not backup or not Path(backup).is_file():
                    raise UpgradeError(f"incomplete transition backup missing: {source}")
                if item.get("sha256") and checksum(Path(backup)) != item["sha256"]:
                    raise UpgradeError(
                        f"incomplete transition backup checksum mismatch: {backup}")
                shutil.copy2(backup, source)
            else:
                source.unlink(missing_ok=True)
        shutil.rmtree(manifest.get("work_dir", ""), ignore_errors=True)
        manifest["stage"] = "recovered_rollback"
        write_manifest(manifest_path, manifest)


def verify(path: Path, database_name: str, expected: SchemaVersion) -> None:
    conn = sqlite_runtime.connect(path, "schema_upgrade")
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
        raise UpgradeError(
            f"{path}: expected V{expected.major}.{expected.minor}, got {actual}")


def replace(source: Path, shadow: Path) -> None:
    conn = sqlite_runtime.connect(shadow, "schema_upgrade")
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise UpgradeError(f"shadow WAL is busy: {shadow}")
    finally:
        conn.close()
    os.replace(shadow, source)
    Path(str(source) + "-wal").unlink(missing_ok=True)
    Path(str(source) + "-shm").unlink(missing_ok=True)


def rebuild_snapshot(proxy_path: Path,
                     snapshot_name: str = "token-board_config_snapshot.db") -> None:
    snapshot = proxy_path.parent / snapshot_name
    temporary = snapshot.with_name(snapshot.name + ".upgrade-new")
    temporary.unlink(missing_ok=True)
    copy_sqlite(proxy_path, temporary)
    conn = sqlite_runtime.connect(temporary, "snapshot_restore")
    try:
        for table in ("request_attempts", "request_log",
                      "billing_period_charges",
                      "agent_subscription_period_charges",
                      "agent_subscription_charge_allocations", "fx_rates"):
            if table_exists(conn, table):
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
    finally:
        conn.close()
    os.replace(temporary, snapshot)
