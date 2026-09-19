"""Resume a V0 to V1 transition from its durable manifest."""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

from transition_common import (migration_locks, migrate, prepare_shadow,
                               read_usage_spool, source_version)
from transition_runtime import (
    atomic_replace,
    rebuild_config_snapshot,
    validate_backups,
    write_manifest,
)
from verify import verify_dashboard, verify_proxy


def resume_transition(
    path: Path,
    transform_proxy,
    transform_dashboard,
) -> None:
    """Continue any durable manifest stage, including pre-verification stops."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    proxy = Path(manifest["token_board_db"])
    dashboard = Path(manifest["dashboard_db"])
    schema_root = Path(manifest["schema_dir"])
    source_tz = ZoneInfo(manifest["timezone"])
    validate_backups(manifest)
    with migration_locks(proxy, dashboard):
        stage = manifest["stage"]
        if stage == "complete":
            return
        if stage == "rolled_back":
            raise RuntimeError("cannot resume a rolled-back transition")
        if stage == "dry_run_complete":
            raise RuntimeError("dry-run manifest cannot be resumed with replacement")
        if stage == "started":
            raise RuntimeError("transition manifest has no durable backup stage")
        if stage == "backed_up":
            if manifest.get("strategy") == "rebuild-shadow":
                for name in ("token-board", "dashboard"):
                    shadow = Path(manifest["shadows"][name])
                    if not shadow.exists():
                        migrate(str(shadow), str(schema_root), name)
            else:
                if not Path(manifest["shadows"]["token-board"]).exists():
                    prepare_shadow(proxy, "token-board", schema_root)
                if not Path(manifest["shadows"]["dashboard"]).exists():
                    prepare_shadow(dashboard, "dashboard", schema_root)
            manifest["stage"] = "shadows_created"
            write_manifest(path, manifest)
            stage = manifest["stage"]
        if stage == "shadows_created":
            source_version(proxy, 19)
            source_version(dashboard, 6)
            spool_records = read_usage_spool(proxy)
            mapping = transform_proxy(
                proxy, Path(manifest["shadows"]["token-board"]), source_tz,
                spool_records)
            transform_dashboard(
                dashboard, Path(manifest["shadows"]["dashboard"]), proxy, mapping,
                mapping.get("credential_masks"))
            manifest["stage"] = "transformed"
            write_manifest(path, manifest)
            stage = manifest["stage"]
        if stage == "transformed":
            manifest["verification"] = {
                "token-board": verify_proxy(
                    proxy, Path(manifest["shadows"]["token-board"]),
                    spool_records=read_usage_spool(proxy)),
                "dashboard": verify_dashboard(
                    dashboard, Path(manifest["shadows"]["dashboard"])),
            }
            manifest["stage"] = "verified"
            write_manifest(path, manifest)
            stage = manifest["stage"]
        if stage == "verified":
            atomic_replace(proxy, Path(manifest["shadows"]["token-board"]),
                           path, manifest, "token_board")
            stage = manifest["stage"]
        if stage == "token_board_replaced":
            atomic_replace(dashboard, Path(manifest["shadows"]["dashboard"]),
                           path, manifest, "dashboard")
            stage = manifest["stage"]
        if stage == "dashboard_replaced":
            manifest["config_snapshot"] = str(rebuild_config_snapshot(proxy))
            manifest["stage"] = "snapshot_rebuilt"
            write_manifest(path, manifest)
            stage = manifest["stage"]
        if stage == "snapshot_rebuilt":
            manifest["stage"] = "complete"
            write_manifest(path, manifest)
