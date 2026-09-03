"""Descriptor-driven transition runner and atomic publication helpers."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.migrations import (
    DASHBOARD_DATABASE_NAME,
    TOKEN_BOARD_DATABASE_NAME,
    SchemaVersion,
    apply_sql_migrations,
)
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
from .transition_api import TransitionContext
from .transition_registry import (
    Transition,
    TransitionRoute,
    discover as discover_transitions,
    record_transition,
    transition_record,
)


def _load_transition(transition: Transition):
    expected_databases = {TOKEN_BOARD_DATABASE_NAME, DASHBOARD_DATABASE_NAME}
    if set(transition.databases) != expected_databases:
        raise UpgradeError(
            f"transition {transition.transition_id} must declare "
            f"{sorted(expected_databases)}, got {transition.databases}")
    module = transition.load()
    if getattr(module, "TRANSITION_ID", transition.transition_id) != transition.transition_id:
        raise UpgradeError(
            f"transition id mismatch in {transition.entrypoint}: "
            f"expected {transition.transition_id}")
    for name in ("apply", "verify"):
        if not callable(getattr(module, name, None)):
            raise UpgradeError(
                f"transition has no {name}() function: {transition.entrypoint}")
    return module


def _published_databases(scope: str) -> tuple[str, ...]:
    if scope == "local-pair":
        return TOKEN_BOARD_DATABASE_NAME, DASHBOARD_DATABASE_NAME
    if scope == "token-board-artifact":
        return (TOKEN_BOARD_DATABASE_NAME,)
    if scope == "dashboard-artifact":
        return (DASHBOARD_DATABASE_NAME,)
    raise UpgradeError(f"unknown transition scope: {scope}")


def _complete_for_scope(paths: dict[str, Path], transition: Transition,
                        scope: str) -> bool:
    names = _published_databases(scope)
    return all(
        name in paths and transition_record(paths[name], transition) is not None
        for name in names
    )


def select_transitions(
        schema_root: Path, scope: str, versions: dict[str, SchemaVersion],
        paths: dict[str, Path]) -> list[tuple[Transition, object]]:
    """Load transitions selected only by the current version vector."""
    selected: list[tuple[Transition, object]] = []
    for transition in discover_transitions(schema_root):
        matches = transition.matching_routes(scope, versions)
        if not matches:
            continue
        if len(matches) != 1:
            raise UpgradeError(
                f"transition {transition.transition_id} has ambiguous routes "
                f"for {scope} at {versions}")
        if _complete_for_scope(paths, transition, scope):
            continue
        selected.append((transition, _load_transition(transition)))
    return selected


def pending_transitions(
        proxy: Path, dashboard: Path, schema_root: Path,
        proxy_version: SchemaVersion,
        dashboard_version: SchemaVersion,
        scope: str = "local-pair") -> list[tuple[Transition, object]]:
    """Return descriptor routes selected by the current database versions."""
    versions = {
        TOKEN_BOARD_DATABASE_NAME: proxy_version,
        DASHBOARD_DATABASE_NAME: dashboard_version,
    }
    paths = {
        TOKEN_BOARD_DATABASE_NAME: proxy,
        DASHBOARD_DATABASE_NAME: dashboard,
    }
    return select_transitions(schema_root, scope, versions, paths)


def _route_for(transition: Transition, scope: str,
               versions: dict[str, SchemaVersion]) -> TransitionRoute:
    matches = transition.matching_routes(scope, versions)
    if len(matches) != 1:
        raise UpgradeError(
            f"transition {transition.transition_id} has {len(matches)} routes "
            f"for {scope} at {versions}")
    return matches[0]


def _prepare_copy_shadow(path: Path, database_name: str,
                         target: SchemaVersion, schema_root: Path) -> None:
    current = inspect_version(path, database_name)
    if current is None:
        raise UpgradeError(f"shadow has no schema version: {path}")
    if current.major != target.major:
        raise UpgradeError(
            f"cannot prepare {database_name} from V{current.major}.{current.minor} "
            f"to V{target.major}.{target.minor}")
    if current > target:
        # A paired barrier can be reached from either side when the other
        # database has already crossed its SQL edge.  Never downgrade a
        # shadow; the plugin receives the actual prepared shape.
        return
    if current != target:
        apply_sql_migrations(
            str(path), str(schema_root), database_name, target=target)


def _resolved_targets(route: TransitionRoute, schema_root: Path,
                      names: set[str]) -> dict[str, SchemaVersion]:
    result = {}
    for name in names:
        target = route.target.get(name)
        if target is not None:
            result[name] = target.resolve(schema_root, name)
    return result


def _context(route: TransitionRoute, scope: str, schema_root: Path,
             source_timezone: ZoneInfo, versions: dict[str, SchemaVersion],
             sources: dict[str, Path], shadows: dict[str, Path],
             metadata: dict) -> TransitionContext:
    names = set(shadows) | set(sources)
    prepare_versions = {}
    for name in names:
        target = route.prepare.get(name)
        if target is not None:
            prepare_versions[name] = target.resolve(
                schema_root, name, versions.get(name))
        elif name in versions:
            prepare_versions[name] = versions[name]
    target_versions = _resolved_targets(route, schema_root, names)
    return TransitionContext(
        scope=scope,
        schema_root=schema_root,
        source_timezone=source_timezone,
        versions=versions,
        sources=sources,
        shadows=shadows,
        prepare_versions=prepare_versions,
        target_versions=target_versions,
        metadata=metadata,
    )


def _apply_shadows(
        source_paths: dict[str, Path], shadow_paths: dict[str, Path],
        schema_root: Path, transitions: list[tuple[Transition, object]],
        versions: dict[str, SchemaVersion], scope: str,
        source_timezone: ZoneInfo,
        metadata: dict | None = None,
        generation_id: str | None = None) -> dict[str, dict]:
    """Apply selected plugins and final SQL to writable shadow files."""
    metadata = metadata or {}
    reports: dict[str, dict] = {}
    contexts: list[tuple[Transition, object, TransitionContext]] = []
    for transition, module in transitions:
        route = _route_for(transition, scope, versions)
        if transition.strategy == "shadow-barrier":
            for name, target in route.prepare.items():
                if name in shadow_paths:
                    _prepare_copy_shadow(
                        shadow_paths[name], name,
                        target.resolve(schema_root, name, versions.get(name)),
                        schema_root)
        elif transition.strategy != "rebuild-shadow":
            raise UpgradeError(
                f"unknown strategy {transition.strategy!r} for "
                f"transition {transition.transition_id}")
        context = _context(
            route, scope, schema_root, source_timezone, versions,
            source_paths, shadow_paths, metadata)
        module.apply(context)
        contexts.append((transition, module, context))

    for name, shadow in shadow_paths.items():
        target = latest_version(schema_root, name, 1)
        apply_sql_migrations(str(shadow), str(schema_root), name, target=target)

    generation_id = generation_id or uuid.uuid4().hex
    for transition, module, context in contexts:
        reports[transition.transition_id] = module.verify(context) or {}
        for name in _published_databases(scope):
            if name in shadow_paths:
                record_transition(shadow_paths[name], transition, generation_id)

    for name, shadow in shadow_paths.items():
        verify(shadow, name, latest_version(schema_root, name, 1))
    return reports


def _backup_paths(proxy: Path, dashboard: Path) -> list[Path]:
    paths = [proxy, dashboard]
    for base in (proxy, dashboard):
        paths.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm"))
    paths.append(Path(str(proxy) + ".request-log.spool"))
    paths.extend(proxy.parent / name for name in (
        "token-board_config_snapshot.db", "token-board_config_snapshot.db-wal",
        "token-board_config_snapshot.db-shm", "config_snapshot.db",
        "config_snapshot.json", "sync_config.json",
        "config_snapshot.db-wal", "config_snapshot.db-shm"))
    return paths


def _manifest(proxy: Path, dashboard: Path, schema_root: Path,
              work_dir: Path, backup_dir: Path,
              transitions: list[tuple[Transition, object]], strategy: str,
              prefix: str) -> tuple[Path, dict]:
    stamp = now()
    manifest_path = proxy.parent / f"{prefix}-{stamp}.manifest.json"
    manifest = {
        "transition": [item.transition_id for item, _module in transitions],
        "strategy": strategy,
        "scope": "local-pair",
        "stage": "started",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "token_board_db": str(proxy),
        "dashboard_db": str(dashboard),
        "schema_dir": str(schema_root),
        "work_dir": str(work_dir),
        "backup_dir": str(backup_dir),
        "shadows": {
            TOKEN_BOARD_DATABASE_NAME: str(work_dir / "token-board.v1-shadow.db"),
            DASHBOARD_DATABASE_NAME: str(work_dir / "dashboard.v1-shadow.db"),
        },
    }
    write_manifest(manifest_path, manifest)
    manifest["backups"] = backup_files(_backup_paths(proxy, dashboard), backup_dir)
    manifest["stage"] = "backed_up"
    write_manifest(manifest_path, manifest)
    return manifest_path, manifest


def _run_copy_pair(proxy: Path, dashboard: Path, schema_root: Path,
                   proxy_version: SchemaVersion,
                   dashboard_version: SchemaVersion,
                   transitions: list[tuple[Transition, object]],
                   source_timezone: ZoneInfo) -> UpgradeResult:
    work_dir = proxy.parent / f"auto-v1-compound-{now()}.work"
    backup_dir = proxy.parent / f"auto-v1-compound-{now()}.backup"
    work_dir.mkdir(parents=True, exist_ok=False)
    manifest_path, manifest = _manifest(
        proxy, dashboard, schema_root, work_dir, backup_dir,
        transitions, "shadow-barrier", "auto-v1-compound")
    proxy_shadow = work_dir / "token-board.v1-shadow.db"
    dashboard_shadow = work_dir / "dashboard.v1-shadow.db"
    copy_sqlite(proxy, proxy_shadow)
    copy_sqlite(dashboard, dashboard_shadow)
    manifest["shadows"] = {
        TOKEN_BOARD_DATABASE_NAME: str(proxy_shadow),
        DASHBOARD_DATABASE_NAME: str(dashboard_shadow),
    }
    manifest["stage"] = "shadows_created"
    write_manifest(manifest_path, manifest)
    generation_id = uuid.uuid4().hex
    reports = _apply_shadows(
        {TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
         DASHBOARD_DATABASE_NAME: dashboard_shadow},
        {TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
         DASHBOARD_DATABASE_NAME: dashboard_shadow},
        schema_root, transitions,
        {TOKEN_BOARD_DATABASE_NAME: proxy_version,
         DASHBOARD_DATABASE_NAME: dashboard_version},
        "local-pair", source_timezone, generation_id=generation_id)
    manifest["verification"] = reports
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
    return UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version,
                         current, True, str(manifest_path))


def _run_rebuild_pair(proxy: Path, dashboard: Path, schema_root: Path,
                      proxy_version: SchemaVersion,
                      dashboard_version: SchemaVersion,
                      transitions: list[tuple[Transition, object]],
                      source_timezone: ZoneInfo, apply: bool = True,
                      manifest_prefix: str = "auto-v0-to-v1",
                      inject_failure: str | None = None,
                      manifest_extra: dict | None = None) -> UpgradeResult:
    work_dir = proxy.parent / f"auto-v0-to-v1-{now()}.work"
    backup_dir = proxy.parent / f"auto-v0-to-v1-{now()}.backup"
    work_dir.mkdir(parents=True, exist_ok=False)
    manifest_path, manifest = _manifest(
        proxy, dashboard, schema_root, work_dir, backup_dir,
        transitions, "rebuild-shadow", manifest_prefix)
    if manifest_extra:
        manifest.update(manifest_extra)
        write_manifest(manifest_path, manifest)

    def inject(stage: str) -> None:
        if inject_failure == stage:
            raise RuntimeError(f"injected transition failure at stage={stage}")

    inject("backed_up")
    proxy_source = work_dir / "token-board.v0-source.db"
    dashboard_source = work_dir / "dashboard.v0-source.db"
    copy_sqlite(proxy, proxy_source)
    copy_sqlite(dashboard, dashboard_source)
    versions = {
        TOKEN_BOARD_DATABASE_NAME: proxy_version,
        DASHBOARD_DATABASE_NAME: dashboard_version,
    }
    route = _route_for(transitions[0][0], "local-pair", versions)
    for name, source, version in (
            (TOKEN_BOARD_DATABASE_NAME, proxy_source, proxy_version),
            (DASHBOARD_DATABASE_NAME, dashboard_source, dashboard_version)):
        target = route.prepare[name].resolve(schema_root, name)
        _prepare_copy_shadow(source, name, target, schema_root)
    proxy_shadow = work_dir / "token-board.v1-shadow.db"
    dashboard_shadow = work_dir / "dashboard.v1-shadow.db"
    apply_sql_migrations(str(proxy_shadow), str(schema_root), TOKEN_BOARD_DATABASE_NAME)
    apply_sql_migrations(str(dashboard_shadow), str(schema_root), DASHBOARD_DATABASE_NAME)
    manifest["sources"] = {
        TOKEN_BOARD_DATABASE_NAME: str(proxy_source),
        DASHBOARD_DATABASE_NAME: str(dashboard_source),
    }
    manifest["shadows"] = {
        TOKEN_BOARD_DATABASE_NAME: str(proxy_shadow),
        DASHBOARD_DATABASE_NAME: str(dashboard_shadow),
    }
    spool_path = Path(str(proxy) + ".request-log.spool")
    spool_info = dict(manifest.get("spool") or {})
    spool_info.setdefault("path", str(spool_path))
    spool_info.setdefault("records", None)
    manifest["spool"] = spool_info
    manifest["stage"] = "shadows_created"
    write_manifest(manifest_path, manifest)
    inject("shadows_created")
    generation_id = uuid.uuid4().hex
    reports = _apply_shadows(
        {TOKEN_BOARD_DATABASE_NAME: proxy_source,
         DASHBOARD_DATABASE_NAME: dashboard_source},
        {TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
         DASHBOARD_DATABASE_NAME: dashboard_shadow},
        schema_root, transitions, versions, "local-pair", source_timezone,
        {"spool_proxy_path": proxy}, generation_id)
    manifest["verification"] = reports
    manifest["generation_id"] = generation_id
    manifest["stage"] = "verified"
    write_manifest(manifest_path, manifest)
    inject("transformed")
    inject("verified")
    if not apply:
        manifest["stage"] = "dry_run_complete"
        write_manifest(manifest_path, manifest)
        shutil.rmtree(work_dir, ignore_errors=True)
        return UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version,
                             proxy_version, False, str(manifest_path))
    replace(proxy, proxy_shadow)
    manifest["stage"] = "token_board_replaced"
    write_manifest(manifest_path, manifest)
    inject("token_board_replaced")
    replace(dashboard, dashboard_shadow)
    manifest["stage"] = "dashboard_replaced"
    write_manifest(manifest_path, manifest)
    inject("dashboard_replaced")
    rebuild_snapshot(proxy)
    legacy_snapshot = proxy.parent / "config_snapshot.db"
    if legacy_snapshot.exists():
        rebuild_snapshot(proxy, "config_snapshot.db")
    manifest["stage"] = "snapshot_rebuilt"
    write_manifest(manifest_path, manifest)
    inject("snapshot_rebuilt")
    Path(str(proxy) + ".request-log.spool").unlink(missing_ok=True)
    manifest["stage"] = "complete"
    write_manifest(manifest_path, manifest)
    shutil.rmtree(work_dir, ignore_errors=True)
    current = inspect_version(proxy, TOKEN_BOARD_DATABASE_NAME)
    assert current is not None
    return UpgradeResult(str(proxy), TOKEN_BOARD_DATABASE_NAME, proxy_version,
                         current, True, str(manifest_path))


def run(proxy: Path, dashboard: Path, schema_root: Path,
        proxy_version: SchemaVersion, dashboard_version: SchemaVersion,
        transitions: list[tuple[Transition, object]],
        source_timezone: str = "Asia/Shanghai", apply: bool = True,
        manifest_prefix: str | None = None,
        inject_failure: str | None = None,
        recover_on_error: bool = True,
        manifest_extra: dict | None = None) -> UpgradeResult:
    """Run selected transition plugins against a local database pair."""
    if not transitions:
        raise UpgradeError("transition runner requires at least one transition")
    timezone = ZoneInfo(source_timezone)
    try:
        if any(item.strategy == "rebuild-shadow" for item, _module in transitions):
            prefix = manifest_prefix or "auto-v0-to-v1"
            return _run_rebuild_pair(
                proxy, dashboard, schema_root, proxy_version, dashboard_version,
                transitions, timezone, apply, prefix,
                inject_failure, manifest_extra)
        return _run_copy_pair(
            proxy, dashboard, schema_root, proxy_version, dashboard_version,
            transitions, timezone)
    except Exception:
        if not recover_on_error:
            raise
        try:
            recover_incomplete_manifests(proxy.parent)
        except Exception as recovery_error:
            raise UpgradeError(
                f"transition rollback failed: {recovery_error}") from recovery_error
        raise


def apply_artifact_pair(proxy_shadow: Path, dashboard_shadow: Path,
                        schema_root: Path,
                        transitions: list[tuple[Transition, object]],
                        scope: str = "dashboard-artifact",
                        source_timezone: str = "Asia/Shanghai") -> None:
    """Apply selected transitions to artifact shadows without publishing proxy."""
    proxy_version = inspect_version(proxy_shadow, TOKEN_BOARD_DATABASE_NAME)
    dashboard_version = inspect_version(dashboard_shadow, DASHBOARD_DATABASE_NAME)
    if proxy_version is None or dashboard_version is None:
        raise UpgradeError("artifact pair must contain versioned databases")
    _apply_shadows(
        {TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
         DASHBOARD_DATABASE_NAME: dashboard_shadow},
        {TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
         DASHBOARD_DATABASE_NAME: dashboard_shadow},
        schema_root, transitions,
        {TOKEN_BOARD_DATABASE_NAME: proxy_version,
         DASHBOARD_DATABASE_NAME: dashboard_version},
        scope, ZoneInfo(source_timezone), generation_id=uuid.uuid4().hex)
