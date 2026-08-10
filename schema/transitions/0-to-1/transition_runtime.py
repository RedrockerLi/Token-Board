"""Backup, atomic replacement, resume and rollback for V0→V1."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from transition_common import (
    assert_offline_and_checkpoint, checksum, migration_locks,
)


def write_manifest(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def backup_files(proxy: Path, dashboard: Path, backup_dir: Path) -> list[dict]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    candidates = [proxy, dashboard]
    for base in (proxy, dashboard):
        candidates.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm"))
    candidates.append(Path(str(proxy) + ".request-log.spool"))
    candidates.extend((proxy.parent / name) for name in (
        "sync_config.json", "config_snapshot.json", "config_snapshot.db",
        "config_snapshot.db-wal", "config_snapshot.db-shm"))
    result = []
    for source in candidates:
        if not source.is_file():
            result.append({"source": str(source), "existed": False})
            continue
        destination = backup_dir / source.name
        shutil.copy2(source, destination)
        result.append({"source": str(source), "backup": str(destination),
                       "sha256": checksum(destination), "size": destination.stat().st_size,
                       "existed": True})
    return result


def atomic_replace(source: Path, shadow: Path, manifest_path: Path,
                   manifest: dict, label: str) -> None:
    conn = sqlite3.connect(shadow, isolation_level=None)
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise RuntimeError(f"shadow WAL checkpoint busy: {shadow}: {result}")
    finally:
        conn.close()
    os.replace(shadow, source)
    # The source database may have empty-but-live V0 WAL/SHM sidecars after
    # the preflight checkpoint.  They belong to a different database image
    # and must never be opened alongside the replaced V1 main file.  Both
    # paths are represented in the manifest and rollback restores them when
    # they existed before the transition.
    Path(str(source) + "-wal").unlink(missing_ok=True)
    Path(str(source) + "-shm").unlink(missing_ok=True)
    manifest["stage"] = f"{label}_replaced"
    write_manifest(manifest_path, manifest)


def rebuild_config_snapshot(proxy: Path) -> Path:
    """Atomically publish a config-only V1 snapshot derived from proxy DB."""
    snapshot = proxy.parent / "config_snapshot.db"
    temporary = proxy.parent / "config_snapshot.db.v1-new"
    temporary.unlink(missing_ok=True)
    source_conn = sqlite3.connect(proxy)
    snapshot_conn = sqlite3.connect(temporary)
    try:
        source_conn.backup(snapshot_conn)
        for table in ("request_attempts", "request_log",
                      "billing_period_charges", "fx_rates"):
            snapshot_conn.execute(f"DELETE FROM {table}")
        snapshot_conn.commit()
    finally:
        snapshot_conn.close()
        source_conn.close()
    os.replace(temporary, snapshot)
    return snapshot


def validate_backups(manifest: dict) -> None:
    for item in manifest.get("backups", []):
        if not item.get("existed", True):
            continue
        path = Path(item["backup"])
        if not path.is_file() or checksum(path) != item["sha256"]:
            raise RuntimeError(f"backup missing or checksum changed: {path}")


def rollback_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_backups(manifest)
    proxy = Path(manifest["proxy_db"])
    dashboard = Path(manifest["dashboard_db"])
    with migration_locks(proxy, dashboard):
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        staged_files = []
        for item in manifest["backups"]:
            source = Path(item["source"])
            if not item.get("existed", True):
                source.unlink(missing_ok=True)
                continue
            staged = source.with_name(source.name + ".v0-restore")
            shutil.copy2(item["backup"], staged)
            if checksum(staged) != item["sha256"]:
                raise RuntimeError(f"restore checksum failed: {source}")
            staged_files.append((source, staged))
        for source, staged in staged_files:
            os.replace(staged, source)
        snapshot = manifest.get("config_snapshot")
        backed_sources = {item["source"] for item in manifest["backups"]
                          if item.get("existed", True)}
        if snapshot and snapshot not in backed_sources:
            Path(snapshot).unlink(missing_ok=True)
    manifest["stage"] = "rolled_back"
    write_manifest(path, manifest)


def resume_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_backups(manifest)
    proxy = Path(manifest["proxy_db"])
    dashboard = Path(manifest["dashboard_db"])
    stage = manifest["stage"]
    with migration_locks(proxy, dashboard):
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        if stage in {"verified", "dry_run_complete"}:
            atomic_replace(proxy, Path(manifest["shadows"]["proxy"]),
                           path, manifest, "proxy")
            stage = manifest["stage"]
        if stage == "proxy_replaced":
            atomic_replace(dashboard, Path(manifest["shadows"]["dashboard"]),
                           path, manifest, "dashboard")
            stage = manifest["stage"]
        if stage == "dashboard_replaced":
            manifest["config_snapshot"] = str(rebuild_config_snapshot(proxy))
            manifest["stage"] = "complete"
            write_manifest(path, manifest)
        elif stage != "complete":
            raise RuntimeError(f"manifest stage cannot be resumed automatically: {stage}")
