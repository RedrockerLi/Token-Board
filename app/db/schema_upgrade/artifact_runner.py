"""Upgrade one local or downloaded database artifact through transitions."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.migrations import (
    DASHBOARD_DATABASE_NAME,
    SchemaVersion,
    TOKEN_BOARD_DATABASE_NAME,
    apply_sql_migrations,
)
from .artifact import strip_runtime_artifact
from .compound import (
    _apply_shadows,
    _prepare_copy_shadow,
    _route_for,
    apply_artifact_pair,
    select_transitions,
)
from .engine_core import (
    UpgradeError,
    UpgradeResult,
    inspect_version,
    latest_version,
    replace,
    verify,
)
from .sqlite_utils import copy_sqlite


def upgrade_artifact(path: Path, database_name: str, schema_root: Path,
                     source_timezone: str = "Asia/Shanghai",
                     local_token_board_path: Path | None = None,
                     configuration_only: bool = False) -> UpgradeResult:
    """Upgrade one local or downloaded artifact through the plugin runner."""
    current = inspect_version(path, database_name)
    if current is None:
        apply_sql_migrations(str(path), str(schema_root), database_name)
        if configuration_only:
            strip_runtime_artifact(path, database_name)
        latest = inspect_version(path, database_name)
        assert latest is not None
        return UpgradeResult(str(path), database_name, None, latest, True)

    work = path.parent / f".{path.name}.upgrade-{uuid.uuid4().hex}"
    work.mkdir(mode=0o700)
    try:
        if current.major == 0:
            if database_name == TOKEN_BOARD_DATABASE_NAME:
                scope = "token-board-artifact"
                versions = {database_name: current}
                paths = {database_name: path}
            else:
                if local_token_board_path is None:
                    raise UpgradeError("dashboard V0 upgrade requires local V1 proxy identity")
                proxy_version = inspect_version(
                    local_token_board_path, TOKEN_BOARD_DATABASE_NAME)
                if proxy_version is None or proxy_version.major not in (1, 2):
                    raise UpgradeError("dashboard V0 upgrade requires a current V1/V2 proxy")
                scope = "dashboard-artifact"
                versions = {
                    TOKEN_BOARD_DATABASE_NAME: proxy_version,
                    DASHBOARD_DATABASE_NAME: current,
                }
                paths = {
                    TOKEN_BOARD_DATABASE_NAME: local_token_board_path,
                    DASHBOARD_DATABASE_NAME: path,
                }
            transitions = select_transitions(schema_root, scope, versions, paths)
            if len(transitions) != 1:
                raise UpgradeError(
                    f"expected one V0 transition for {scope}, found "
                    f"{[item.transition_id for item, _module in transitions]}")
            transition, _module = transitions[0]
            route = _route_for(transition, scope, versions)
            source = work / f"{database_name}.v0-source.db"
            shadow = work / f"{database_name}.v1-shadow.db"
            copy_sqlite(path, source)
            prepare = route.prepare[database_name].resolve(
                schema_root, database_name, current)
            _prepare_copy_shadow(source, database_name, prepare, schema_root)
            landing = (SchemaVersion(1, 13)
                       if database_name == TOKEN_BOARD_DATABASE_NAME
                       else latest_version(schema_root, database_name, 1))
            apply_sql_migrations(
                str(shadow), str(schema_root), database_name,
                target=landing)
            source_paths = {database_name: source}
            shadow_paths = {database_name: shadow}
            final_targets = {database_name: landing}
            metadata = {}
            if database_name == TOKEN_BOARD_DATABASE_NAME:
                metadata["spool_proxy_path"] = path
            else:
                source_paths[TOKEN_BOARD_DATABASE_NAME] = local_token_board_path
                proxy_shadow = work / "local-token-board.v1-shadow.db"
                copy_sqlite(local_token_board_path, proxy_shadow)
                shadow_paths[TOKEN_BOARD_DATABASE_NAME] = proxy_shadow
                proxy_version = inspect_version(
                    local_token_board_path, TOKEN_BOARD_DATABASE_NAME)
                assert proxy_version is not None
                final_targets[TOKEN_BOARD_DATABASE_NAME] = (
                    SchemaVersion(1, 13)
                    if proxy_version.minor <= 13
                    else latest_version(schema_root, TOKEN_BOARD_DATABASE_NAME, 1)
                )
            _apply_shadows(
                source_paths, shadow_paths, schema_root, transitions, versions,
                scope, ZoneInfo(source_timezone), metadata,
                target_versions=final_targets)

            if database_name == TOKEN_BOARD_DATABASE_NAME:
                # The legacy V0 transformer lands on V1.13 because its
                # implementation still writes the pre-flattened V1 shape.
                # Immediately run any newly applicable V1 artifact routes in
                # the same temporary upgrade, without changing the published
                # 0-to-1 transition plugin.
                v1_version = inspect_version(shadow, database_name)
                assert v1_version is not None
                v1_paths = {database_name: shadow}
                v1_transitions = select_transitions(
                    schema_root, "token-board-artifact",
                    {database_name: v1_version}, v1_paths)
                if v1_transitions:
                    v1_source = work / "token-board.v1-source.db"
                    copy_sqlite(shadow, v1_source)
                    _apply_shadows(
                        {database_name: v1_source},
                        {database_name: shadow},
                        schema_root, v1_transitions,
                        {database_name: v1_version},
                        "token-board-artifact", ZoneInfo(source_timezone),
                        generation_id=uuid.uuid4().hex)
            elif database_name == DASHBOARD_DATABASE_NAME:
                # The Token Board shadow is only a companion for dashboard
                # identity conversion, but it may still be V1.13 and carry
                # the old pricing layout. Upgrade that companion through the
                # token-board artifact route so a raw 1.14 migration cannot
                # bypass the pricing transition.
                proxy_shadow = shadow_paths.get(TOKEN_BOARD_DATABASE_NAME)
                if proxy_shadow is not None:
                    proxy_version = inspect_version(
                        proxy_shadow, TOKEN_BOARD_DATABASE_NAME)
                    assert proxy_version is not None
                    v1_paths = {TOKEN_BOARD_DATABASE_NAME: proxy_shadow}
                    v1_transitions = select_transitions(
                        schema_root, "token-board-artifact",
                        {TOKEN_BOARD_DATABASE_NAME: proxy_version}, v1_paths)
                    if v1_transitions:
                        v1_source = work / "token-board.v1-source.db"
                        copy_sqlite(proxy_shadow, v1_source)
                        _apply_shadows(
                            {TOKEN_BOARD_DATABASE_NAME: v1_source},
                            {TOKEN_BOARD_DATABASE_NAME: proxy_shadow},
                            schema_root, v1_transitions,
                            {TOKEN_BOARD_DATABASE_NAME: proxy_version},
                            "token-board-artifact", ZoneInfo(source_timezone),
                            generation_id=uuid.uuid4().hex)
            # V2 artifacts are the only artifacts accepted by runtime
            # services.  A V0 download first needs the historical V0->V1
            # transformer above, but the same shadow must immediately pass
            # through the compound V1->V2 schema migration before it can be
            # published.  This closes the old V0->V1-only escape hatch.
            shadow_version = inspect_version(shadow, database_name)
            if (shadow_version is not None and shadow_version.major == 1 and
                    (schema_root / database_name / "v2").is_dir()):
                apply_sql_migrations(
                    str(shadow), str(schema_root), database_name,
                    target=latest_version(schema_root, database_name, 2))
            if configuration_only:
                strip_runtime_artifact(shadow, database_name)
            replace(path, shadow)
            if database_name == TOKEN_BOARD_DATABASE_NAME:
                Path(str(path) + ".request-log.spool").unlink(missing_ok=True)
            latest = inspect_version(path, database_name)
            assert latest is not None
            return UpgradeResult(str(path), database_name, current, latest, True)

        if current.major not in (1, 2):
            raise UpgradeError(
                f"unsupported {database_name} schema V{current.major}.{current.minor}")
        artifact_shadow = work / f"{database_name}.v{current.major}-shadow.db"
        copy_sqlite(path, artifact_shadow)
        if current.major == 2:
            if database_name == DASHBOARD_DATABASE_NAME and local_token_board_path:
                proxy_version = inspect_version(
                    local_token_board_path, TOKEN_BOARD_DATABASE_NAME)
                if proxy_version is None or proxy_version.major != 2:
                    raise UpgradeError("dashboard V2 upgrade requires a current V2 proxy")
            apply_sql_migrations(
                str(artifact_shadow), str(schema_root), database_name,
                target=latest_version(schema_root, database_name, 2))
        elif database_name == TOKEN_BOARD_DATABASE_NAME:
            versions = {TOKEN_BOARD_DATABASE_NAME: current}
            paths = {TOKEN_BOARD_DATABASE_NAME: path}
            transitions = select_transitions(
                schema_root, "token-board-artifact", versions, paths)
            if transitions:
                _apply_shadows(
                    {TOKEN_BOARD_DATABASE_NAME: path},
                    {TOKEN_BOARD_DATABASE_NAME: artifact_shadow},
                    schema_root, transitions, versions,
                    "token-board-artifact", ZoneInfo(source_timezone),
                    generation_id=uuid.uuid4().hex)
            else:
                apply_sql_migrations(
                    str(artifact_shadow), str(schema_root), database_name)
        elif database_name == DASHBOARD_DATABASE_NAME and local_token_board_path:
            proxy_version = inspect_version(
                local_token_board_path, TOKEN_BOARD_DATABASE_NAME)
            if proxy_version is None or proxy_version.major not in (1, 2):
                raise UpgradeError("dashboard upgrade requires a current V1/V2 proxy")
            proxy_shadow = work / "local-token-board.v1-shadow.db"
            copy_sqlite(local_token_board_path, proxy_shadow)
            dashboard_version = inspect_version(artifact_shadow, DASHBOARD_DATABASE_NAME)
            assert dashboard_version is not None
            versions = {
                TOKEN_BOARD_DATABASE_NAME: proxy_version,
                DASHBOARD_DATABASE_NAME: dashboard_version,
            }
            paths = {
                TOKEN_BOARD_DATABASE_NAME: proxy_shadow,
                DASHBOARD_DATABASE_NAME: artifact_shadow,
            }
            transitions = select_transitions(
                schema_root, "dashboard-artifact", versions, paths)
            if transitions:
                apply_artifact_pair(
                    proxy_shadow, artifact_shadow, schema_root, transitions,
                    "dashboard-artifact", source_timezone)
            else:
                apply_sql_migrations(
                    str(artifact_shadow), str(schema_root), DASHBOARD_DATABASE_NAME)
        else:
            apply_sql_migrations(
                str(artifact_shadow), str(schema_root), database_name)
        # V2 is a compound major transition applied only after the V1 artifact
        # route has finished on the shadow.  It is intentionally never applied
        # to the live file in-place.
        current_shadow = inspect_version(artifact_shadow, database_name)
        assert current_shadow is not None
        if current_shadow.major == 1 and (schema_root / database_name / "v2").is_dir():
            apply_sql_migrations(
                str(artifact_shadow), str(schema_root), database_name,
                target=latest_version(schema_root, database_name, 2))
        expected = latest_version(
            schema_root, database_name,
            inspect_version(artifact_shadow, database_name).major)
        verify(artifact_shadow, database_name, expected)
        if configuration_only:
            strip_runtime_artifact(artifact_shadow, database_name)
        replace(path, artifact_shadow)
        latest = inspect_version(path, database_name)
        assert latest is not None
        return UpgradeResult(str(path), database_name, current, latest,
                             latest != current)
    finally:
        shutil.rmtree(work, ignore_errors=True)
