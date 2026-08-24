"""Descriptor-driven V1 compound transitions and atomic publication."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db.migrations import (DASHBOARD_DATABASE_NAME,
                               TOKEN_BOARD_DATABASE_NAME, SchemaVersion,
                               apply_sql_migrations)
from .engine_core import (
    UpgradeError,
    UpgradeResult,
    backup_files,
    inspect_version,
    latest_version,
    now,
    recover_incomplete_manifests,
    rebuild_snapshot,
    replace,
    verify,
    write_manifest,
)
from .sqlite_utils import copy_sqlite
from .transition_registry import (
    Transition,
    discover as discover_transitions,
    record_transition,
    transition_record,
)


def _load_transition(transition: Transition):
    expected_databases = {TOKEN_BOARD_DATABASE_NAME, DASHBOARD_DATABASE_NAME}
    if set(transition.databases) != expected_databases:
        raise UpgradeError(
            f"V1 compound transition {transition.transition_id} must declare "
            f"{sorted(expected_databases)}, got {transition.databases}")
    module = transition.load()
    if getattr(module, "TRANSITION_ID", transition.transition_id) != transition.transition_id:
        raise UpgradeError(
            f"transition id mismatch in {transition.entrypoint}: "
            f"expected {transition.transition_id}")
    for name in ("needs", "apply", "verify"):
        if not callable(getattr(module, name, None)):
            raise UpgradeError(
                f"transition has no {name}() function: {transition.entrypoint}")
    return module


def pending_transitions(
        proxy: Path, dashboard: Path, schema_root: Path,
        proxy_version: SchemaVersion,
        dashboard_version: SchemaVersion) -> list[tuple[Transition, object]]:
    """Return descriptor-defined transitions whose pair barrier is pending."""
    pending: list[tuple[Transition, object]] = []
    for transition in discover_transitions(schema_root):
        module = _load_transition(transition)
        proxy_record = transition_record(proxy, transition)
        dashboard_record = transition_record(dashboard, transition)
        if proxy_record and dashboard_record:
            continue
        if (proxy_record or dashboard_record or
                module.needs(proxy, dashboard, proxy_version, dashboard_version)):
            pending.append((transition, module))
    return pending


def _apply_shadows(
        proxy_shadow: Path, dashboard_shadow: Path, schema_root: Path,
        transitions: list[tuple[Transition, object]], generation_id: str) -> tuple[SchemaVersion, SchemaVersion]:
    for transition, module in transitions:
        current_proxy = inspect_version(proxy_shadow, TOKEN_BOARD_DATABASE_NAME)
        current_dashboard = inspect_version(dashboard_shadow, "dashboard")
        if current_proxy is None or current_dashboard is None:
            raise UpgradeError(
                f"transition {transition.transition_id} removed schema metadata")
        module.apply(proxy_shadow, dashboard_shadow, schema_root,
                     current_proxy, current_dashboard)

    # A transition can expose an intermediate SQL shape to Python.  Only the
    # fully upgraded and verified pair may cross the publication barrier.
    apply_sql_migrations(str(proxy_shadow), str(schema_root), TOKEN_BOARD_DATABASE_NAME)
    apply_sql_migrations(str(dashboard_shadow), str(schema_root), "dashboard")
    expected_proxy = latest_version(schema_root, TOKEN_BOARD_DATABASE_NAME, 1)
    expected_dashboard = latest_version(schema_root, "dashboard", 1)
    for transition, module in transitions:
        module.verify(proxy_shadow, dashboard_shadow)
        record_transition(proxy_shadow, transition, generation_id)
        record_transition(dashboard_shadow, transition, generation_id)
    verify(proxy_shadow, TOKEN_BOARD_DATABASE_NAME, expected_proxy)
    verify(dashboard_shadow, "dashboard", expected_dashboard)
    return expected_proxy, expected_dashboard


def _shadow_pair(proxy: Path, dashboard: Path, work_dir: Path) -> tuple[Path, Path]:
    proxy_shadow = work_dir / "token-board.v1-shadow.db"
    dashboard_shadow = work_dir / "dashboard.v1-shadow.db"
    copy_sqlite(proxy, proxy_shadow)
    copy_sqlite(dashboard, dashboard_shadow)
    return proxy_shadow, dashboard_shadow


def _backup_paths(proxy: Path, dashboard: Path) -> list[Path]:
    paths = [proxy, dashboard]
    for base in (proxy, dashboard):
        paths.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm"))
    paths.extend(proxy.parent / name for name in (
        "token-board_config_snapshot.db", "token-board_config_snapshot.db-wal",
        "token-board_config_snapshot.db-shm", "config_snapshot.db",
        "config_snapshot.db-wal", "config_snapshot.db-shm"))
    return paths


def _run_impl(proxy: Path, dashboard: Path, schema_root: Path,
              proxy_version: SchemaVersion, dashboard_version: SchemaVersion,
              transitions: list[tuple[Transition, object]]) -> UpgradeResult:
    stamp = now()
    manifest_path = proxy.parent / f"auto-v1-compound-{stamp}.manifest.json"
    work_dir = proxy.parent / f"auto-v1-compound-{stamp}.work"
    backup_dir = proxy.parent / f"auto-v1-compound-{stamp}.backup"
    work_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "transition": [item.transition_id for item, _module in transitions],
        "stage": "started", "created_at": datetime.now(timezone.utc).isoformat(),
        "token_board_db": str(proxy), "dashboard_db": str(dashboard),
        "schema_dir": str(schema_root), "work_dir": str(work_dir),
        "backup_dir": str(backup_dir),
    }
    write_manifest(manifest_path, manifest)
    manifest["backups"] = backup_files(_backup_paths(proxy, dashboard), backup_dir)
    manifest["stage"] = "backed_up"
    write_manifest(manifest_path, manifest)

    proxy_shadow, dashboard_shadow = _shadow_pair(proxy, dashboard, work_dir)
    manifest["shadows"] = {TOKEN_BOARD_DATABASE_NAME: str(proxy_shadow), "dashboard": str(dashboard_shadow)}
    manifest["stage"] = "shadows_created"
    write_manifest(manifest_path, manifest)
    generation_id = uuid.uuid4().hex
    _apply_shadows(proxy_shadow, dashboard_shadow, schema_root, transitions,
                   generation_id)
    manifest["generation_id"] = generation_id
    manifest["stage"] = "verified"
    write_manifest(manifest_path, manifest)

    replace(proxy, proxy_shadow)
    manifest["stage"] = "token_board_replaced"
    write_manifest(manifest_path, manifest)
    replace(dashboard, dashboard_shadow)
    manifest["stage"] = "dashboard_replaced"
    write_manifest(manifest_path, manifest)
    snapshot = proxy.parent / "token-board_config_snapshot.db"
    if snapshot.exists():
        rebuild_snapshot(proxy)
        manifest["stage"] = "snapshot_rebuilt"
        write_manifest(manifest_path, manifest)
    manifest["stage"] = "complete"
    write_manifest(manifest_path, manifest)
    shutil.rmtree(work_dir, ignore_errors=True)
    current = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
    assert current is not None
    return UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version, current, True,
                         str(manifest_path))


def run(proxy: Path, dashboard: Path, schema_root: Path,
        proxy_version: SchemaVersion, dashboard_version: SchemaVersion,
        transitions: list[tuple[Transition, object]]) -> UpgradeResult:
    try:
        return _run_impl(proxy, dashboard, schema_root, proxy_version,
                         dashboard_version, transitions)
    except Exception:
        try:
            recover_incomplete_manifests(proxy.parent)
        except Exception as recovery_error:
            raise UpgradeError(
                f"compound transition rollback failed: {recovery_error}") \
                from recovery_error
        raise


def apply_artifact_pair(proxy_shadow: Path, dashboard_shadow: Path,
                        schema_root: Path,
                        transitions: list[tuple[Transition, object]]) -> None:
    """Apply the same barrier to a downloaded artifact without publishing proxy."""
    _apply_shadows(proxy_shadow, dashboard_shadow, schema_root, transitions,
                   uuid.uuid4().hex)
