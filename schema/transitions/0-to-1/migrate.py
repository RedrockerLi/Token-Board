#!/usr/bin/env python3
"""Offline, resumable Token Board V0→V1 shadow-database transition.

The command is a dry run unless ``--apply`` is supplied.  It never transforms
the live files in place: two V1 shadows are built, independently verified, and
only then atomically replace the originals.  A manifest records every stage.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))

from verify import verify_dashboard, verify_proxy  # noqa: E402

_migration_spec = importlib.util.spec_from_file_location(
    "token_board_migrations", REPO / "app" / "db" / "migrations.py")
assert _migration_spec is not None and _migration_spec.loader is not None
_migration_module = importlib.util.module_from_spec(_migration_spec)
sys.modules[_migration_spec.name] = _migration_module
_migration_spec.loader.exec_module(_migration_module)
migrate = _migration_module.migrate


NAMESPACE = uuid.UUID("646d9175-a4f5-4caa-aaf4-98362b8fd550")
LEGACY_CREDENTIAL_UUID = "00000000-0000-5000-8000-000000000001"


def stable_uuid(kind: str, *parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join([kind, *(str(p) for p in parts)])))


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return key[:3] + "…"
    return f"{key[:6]}…{key[-4:]}"


def utc_timestamp(value: str | None, source_tz: ZoneInfo) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10:
        return text
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_tz)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def migration_locks(proxy: Path, dashboard: Path):
    handles = []
    try:
        for path in (proxy, dashboard):
            lock = open(str(path) + ".migrate.lock", "a+b")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            handles.append(lock)
        yield
    finally:
        for lock in reversed(handles):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def assert_offline_and_checkpoint(path: Path) -> None:
    conn = sqlite3.connect(path, timeout=1, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise RuntimeError(f"WAL checkpoint busy for {path}: {result}")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"database is still in use: {path}: {exc}") from exc
    finally:
        conn.close()


def assert_spool_empty(proxy: Path) -> None:
    spool = Path(str(proxy) + ".request-log.spool")
    if spool.exists() and spool.stat().st_size:
        raise RuntimeError(
            f"request accounting spool is not empty ({spool.stat().st_size} bytes): {spool}")


def source_version(path: Path, expected_minor: int) -> None:
    conn = sqlite3.connect(path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        major, minor = divmod(version, 10_000)
        if major != 0 or minor != expected_minor:
            raise RuntimeError(
                f"{path} must be V0.{expected_minor}, found V{major}.{minor}")
    finally:
        conn.close()


def shadow_path(source: Path) -> Path:
    return source.with_name(source.name + ".v1-shadow")


def prepare_shadow(source: Path, database_name: str, schema_root: Path) -> Path:
    shadow = shadow_path(source)
    if shadow.exists():
        raise RuntimeError(f"shadow already exists; resume or move it aside: {shadow}")
    migrate(str(shadow), str(schema_root), database_name)
    return shadow


def transform_proxy(source: Path, shadow: Path, source_tz: ZoneInfo) -> dict:
    old = sqlite3.connect(source)
    old.row_factory = sqlite3.Row
    new = sqlite3.connect(shadow)
    new.row_factory = sqlite3.Row
    new.execute("PRAGMA foreign_keys=ON")
    try:
        new.execute("BEGIN IMMEDIATE")
        new.executescript((HERE / "proxy_transform.sql").read_text(encoding="utf-8"))
        accounts = old.execute("SELECT * FROM upstream_accounts ORDER BY id").fetchall()
        real_accounts = [row for row in accounts if not row["is_aggregate"]]
        max_account = max((row["id"] for row in real_accounts), default=0)
        legacy_account_id = max_account + 1

        for row in real_accounts:
            lifecycle = "deleted" if row["deleted_at"] else "active"
            new.execute(
                "INSERT INTO accounts(id,uuid,name,lifecycle_state,valid_from,deleted_at,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (row["id"], stable_uuid("account", row["id"]), row["name"], lifecycle,
                 row["valid_from"], utc_timestamp(row["deleted_at"], source_tz),
                 utc_timestamp(row["created_at"], source_tz),
                 utc_timestamp(row["created_at"], source_tz)),
            )
        new.execute(
            "INSERT INTO accounts(id,uuid,name,lifecycle_state,created_at,updated_at) "
            "VALUES(?,?,?,'disabled',?,?)",
            (legacy_account_id, stable_uuid("account", "legacy-unattributed"),
             "legacy-unattributed", "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
        )

        routable_ids: set[int] = set()
        for row in real_accounts:
            if row["account_type"] == "agent":
                state = old.execute(
                    "SELECT COALESCE(json_group_array(json_object('path',path,'size',size,"
                    "'mtime',mtime,'last_line',last_line,'session_id',session_id,"
                    "'parsed_at',parsed_at)),'[]') FROM codex_import_state").fetchone()[0]
                new.execute(
                    "INSERT INTO account_importers(uuid,account_id,importer_kind,cursor_json,enabled) "
                    "VALUES(?,?,?,?,?)",
                    (stable_uuid("importer", row["id"]), row["id"],
                     row["agent_kind"] or "codex", state, 0 if row["deleted_at"] else 1),
                )
                continue
            routable_ids.add(row["id"])
            new.execute(
                "INSERT INTO upstreams(id,account_id,name,base_url,api_format,auth_scheme,"
                "endpoint_path,max_concurrency,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["id"], row["name"], row["base_url"], row["api_format"],
                 row["auth_header"] or "bearer", row["endpoint_path"],
                 row["max_concurrency"] or 0, 0 if row["deleted_at"] else 1,
                 utc_timestamp(row["created_at"], source_tz),
                 utc_timestamp(row["created_at"], source_tz)),
            )

        legacy_upstream_id = max(routable_ids, default=0) + 1
        new.execute(
            "INSERT INTO upstreams(id,account_id,name,base_url,enabled) VALUES(?,?,?,?,0)",
            (legacy_upstream_id, legacy_account_id, "legacy-unattributed", "http://invalid"),
        )

        route_ids: set[int] = set()
        for row in accounts:
            if row["account_type"] == "agent":
                continue
            route_ids.add(row["id"])
            new.execute(
                "INSERT INTO route_sets(id,uuid,account_id,name,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (row["id"], stable_uuid("route-set", row["id"]),
                 None if row["is_aggregate"] else row["id"], row["name"],
                 0 if row["deleted_at"] else 1,
                 utc_timestamp(row["created_at"], source_tz),
                 utc_timestamp(row["created_at"], source_tz)),
            )
            if not row["is_aggregate"]:
                new.execute(
                    "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id) "
                    "VALUES(?,'*',0,?)", (row["id"], row["id"]),
                )
        for entry in old.execute("SELECT * FROM aggregate_entries ORDER BY sort_order,id"):
            if entry["account_id"] not in route_ids or entry["upstream_account_id"] not in routable_ids:
                continue
            new.execute(
                "INSERT INTO route_rules(route_set_id,model_pattern,priority,upstream_id,"
                "target_model) VALUES(?,?,?,?,?)",
                (entry["account_id"], entry["pattern"] or "*", entry["sort_order"],
                 entry["upstream_account_id"], entry["upstream_model"] or None),
            )

        for row in old.execute("SELECT * FROM local_keys ORDER BY id"):
            if row["account_id"] not in route_ids:
                continue
            new.execute(
                "INSERT INTO client_keys(id,uuid,key_value,label,route_set_id,created_at,"
                "last_used_at) VALUES(?,?,?,?,?,?,?)",
                (row["id"], stable_uuid("client-key", row["id"]), row["key_value"],
                 row["label"], row["account_id"], utc_timestamp(row["created_at"], source_tz),
                 utc_timestamp(row["last_used_at"], source_tz)),
            )

        local_keys = old.execute("SELECT * FROM upstream_keys ORDER BY account_id,position,id").fetchall()
        seen_masks: dict[tuple[int, str], int] = {}
        credential_map: dict[int, str] = {}
        for row in local_keys:
            masked = mask_key(row["key_value"])
            identity = (row["account_id"], masked)
            if identity in seen_masks and seen_masks[identity] != row["id"]:
                raise RuntimeError(
                    f"masked credential collision for account {row['account_id']}: {masked}; "
                    f"key ids {seen_masks[identity]} and {row['id']}")
            seen_masks[identity] = row["id"]
            credential_uuid = stable_uuid("credential", row["account_id"], masked)
            credential_map[row["id"]] = credential_uuid
            new.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,"
                "valid_from,created_at,deleted_at) VALUES(?,?,?,?,?,?,?,?)",
                (credential_uuid, row["id"], row["account_id"], row["position"], masked,
                 row["valid_from"], utc_timestamp(row["created_at"], source_tz),
                 utc_timestamp(row["deleted_at"], source_tz)),
            )
            new.execute(
                "INSERT INTO upstream_secrets(credential_uuid,secret_value) VALUES(?,?)",
                (credential_uuid, row["key_value"]),
            )
            new.execute(
                "INSERT INTO migration_credential_map VALUES(?,?)",
                (row["id"], credential_uuid),
            )
        for row in old.execute("SELECT * FROM upstream_keys_cloud ORDER BY account_id,position"):
            identity = (row["account_id"], row["key_masked"])
            if identity in seen_masks:
                continue
            seen_masks[identity] = -1
            credential_uuid = stable_uuid("credential", *identity)
            new.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,"
                "valid_from,created_at,deleted_at) VALUES(?,?,?,?,?,?,?,?)",
                (credential_uuid, 1_000_000_000 + len(seen_masks), row["account_id"],
                 row["position"], row["key_masked"],
                 row["valid_from"], "1970-01-01T00:00:00Z",
                 utc_timestamp(row["deleted_at"], source_tz)),
            )
        new.execute(
            "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,"
            "created_at,disabled_at) VALUES(?,?,?,?,?,?,?)",
            (LEGACY_CREDENTIAL_UUID, 2_000_000_000, legacy_upstream_id, 0,
             "legacy-unattributed",
             "1970-01-01T00:00:00Z", "1970-01-01T00:00:00Z"),
        )

        billing_config = old.execute(
            "SELECT price_change_effective,cancellation_mode FROM plan_billing_config WHERE id=1"
        ).fetchone()
        cancellation = (billing_config["cancellation_mode"] if billing_config else "period_end")
        cancellation = cancellation if cancellation in {"immediate", "period_end"} else "period_end"
        contract_ids: dict[int, int] = {}
        for row in real_accounts:
            recurring = row["account_type"] in {"plan", "agent"}
            scope = "account" if row["account_type"] == "agent" else (
                "credential" if recurring else "account")
            cursor = new.execute(
                "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,"
                "currency,cancellation_policy,valid_from,valid_until) VALUES(?,?,?,?,?,?,?,?)",
                (stable_uuid("contract", row["id"]), row["id"],
                 "recurring" if recurring else "metered", scope, row["currency"] or "CNY",
                 cancellation, row["valid_from"] or "1970-01-01",
                 utc_timestamp(row["deleted_at"], source_tz)),
            )
            contract_ids[row["id"]] = cursor.lastrowid
            new.execute("INSERT INTO migration_account_type VALUES(?,?)", (
                row["id"], "recurring" if recurring else "metered"))
        for price in old.execute("SELECT * FROM plan_price_history ORDER BY id"):
            contract_id = contract_ids.get(price["account_id"])
            if not contract_id:
                continue
            rule = "next_period" if price["effective_mode"] == "next_period" else "immediate"
            new.execute(
                "INSERT INTO billing_rate_events(id,contract_id,recurring_price,effective_at,"
                "effective_rule,created_at) VALUES(?,?,?,?,?,?)",
                (price["id"], contract_id, price["monthly_price"],
                 utc_timestamp(price["changed_at"], source_tz), rule,
                 utc_timestamp(price["changed_at"], source_tz)),
            )

        rate_map: dict[int, int] = {}
        for priority, price in enumerate(old.execute("SELECT * FROM model_pricing ORDER BY id")):
            rule_id = new.execute(
                "INSERT INTO pricing_rules(id,model_pattern,priority) VALUES(?,?,?)",
                (price["id"], price["model_pattern"], priority),
            ).lastrowid
            rate_id = new.execute(
                "INSERT INTO pricing_rates(pricing_rule_id,input_price,cache_read_price,"
                "output_price,currency,valid_from) VALUES(?,?,?,?,?,'1970-01-01T00:00:00Z')",
                (rule_id, price["input_price"],
                 price["cache_read_price"] if price["cache_read_price"] is not None else price["input_price"],
                 price["output_price"], price["currency"] or "CNY"),
            ).lastrowid
            rate_map[price["id"]] = rate_id
        for slot in old.execute("SELECT * FROM pricing_slots ORDER BY id"):
            if slot["pricing_id"] in rate_map:
                new.execute(
                    "INSERT INTO pricing_slots(id,pricing_rate_id,start_minute,end_minute,multiplier) "
                    "VALUES(?,?,?,?,?)",
                    (slot["id"], rate_map[slot["pricing_id"]], slot["start_minute"],
                     slot["end_minute"], slot["multiplier"]),
                )
        for fx in old.execute("SELECT * FROM fx_rate"):
            new.execute(
                "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) VALUES(?,?,?,?)",
                (fx["base"], fx["quote"], fx["date"], fx["rate"]),
            )

        for row in old.execute("SELECT * FROM request_log ORDER BY id"):
            key_uuid = credential_map.get(row["upstream_key_id"])
            if row["upstream_key_id"] is not None and key_uuid is None:
                key_uuid = LEGACY_CREDENTIAL_UUID
            client = row["local_key_id"]
            route = None
            if client is not None:
                found = old.execute("SELECT account_id FROM local_keys WHERE id=?", (client,)).fetchone()
                route = found[0] if found and found[0] in route_ids else None
                if route is None:
                    client = None
            account_id = row["account_id"] if row["account_id"] in contract_ids else legacy_account_id
            charge = "metered" if account_id == legacy_account_id else old.execute(
                "SELECT account_type FROM upstream_accounts WHERE id=?", (account_id,)).fetchone()[0]
            billed = row["api_cost"] if charge == "api" else 0.0
            event_id = row["event_id"] or stable_uuid("request", row["id"])
            new.execute(
                "INSERT INTO request_log(id,event_id,source_kind,account_id,route_set_id,"
                "client_key_id,upstream_key_id,credential_uuid,model,prompt_tokens,completion_tokens,"
                "cache_read_tokens,total_tokens,equivalent_cost,billed_usage_cost,is_streaming,"
                "status_code,duration_ms,ttft_ms,generation_ms,output_tps,upstream_ttft_ms,"
                "upstream_duration_ms,attempt_count,fallback_count,requested_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], event_id, "proxy", account_id, route, client,
                 row["upstream_key_id"], key_uuid,
                 row["model"], row["prompt_tokens"] or 0, row["completion_tokens"] or 0,
                 row["cache_read_tokens"] or 0, row["total_tokens"] or 0,
                 row["api_cost"] or 0.0, billed or 0.0,
                 row["is_streaming"] or 0, row["status_code"], row["duration_ms"] or 0,
                 row["ttft_ms"] or 0, row["generation_ms"] or 0, row["output_tps"] or 0,
                 row["upstream_ttft_ms"] or 0, row["upstream_duration_ms"] or 0,
                 row["attempt_count"] or 0, row["fallback_count"] or 0,
                 utc_timestamp(row["requested_at"], source_tz)),
            )
        for row in old.execute("SELECT * FROM request_attempts ORDER BY id"):
            key_uuid = credential_map.get(row["upstream_key_id"])
            if row["upstream_key_id"] is not None and key_uuid is None:
                key_uuid = LEGACY_CREDENTIAL_UUID
            upstream = row["account_id"] if row["account_id"] in routable_ids else legacy_upstream_id
            new.execute(
                "INSERT INTO request_attempts(id,request_log_id,attempt_index,upstream_id,"
                "credential_uuid,account_id,upstream_key_id,status_code,duration_ms,ttft_ms,"
                "is_timeout,error,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["request_log_id"], row["attempt_index"], upstream,
                 key_uuid, row["account_id"], row["upstream_key_id"], row["status_code"],
                 row["duration_ms"] or 0,
                 row["ttft_ms"] or 0, row["is_timeout"] or 0, row["error"],
                 utc_timestamp(row["requested_at"], source_tz)),
            )
        for row in old.execute("SELECT key,value FROM sync_config"):
            new.execute("INSERT INTO sync_settings(key,value) VALUES(?,?)", tuple(row))
        for row in old.execute("SELECT key,value FROM sync_state"):
            new.execute("INSERT INTO sync_state(key,value) VALUES(?,?)", tuple(row))

        new.execute("UPDATE config_state SET generation=generation+1")
        new.commit()
        return {"credential_map": credential_map,
                "legacy_account_id": legacy_account_id,
                "account_types": {row["id"]: row["account_type"] for row in real_accounts}}
    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        new.close()


def transform_dashboard(source: Path, shadow: Path, proxy_source: Path,
                        proxy_mapping: dict) -> None:
    old = sqlite3.connect(source)
    old.row_factory = sqlite3.Row
    proxy = sqlite3.connect(proxy_source)
    proxy.row_factory = sqlite3.Row
    new = sqlite3.connect(shadow)
    new.row_factory = sqlite3.Row
    new.execute("PRAGMA foreign_keys=ON")
    upsert = (HERE / "dashboard_transform.sql").read_text(encoding="utf-8")
    try:
        new.execute("BEGIN IMMEDIATE")
        account_names = {row["account_id"]: row["name"]
                         for row in old.execute("SELECT * FROM accounts")}
        grain_ids = set()
        for table in ("token_usage", "request_usage", "cost_entry", "proxy_plan_summary"):
            grain_ids.update(row[0] for row in old.execute(
                f"SELECT DISTINCT account_id FROM {table}"))
        for account_id in sorted(grain_ids | set(account_names)):
            new.execute(
                "INSERT INTO accounts(account_id,name) VALUES(?,?)",
                (account_id, account_names.get(account_id, f"legacy-{account_id}")),
            )

        grains: dict[tuple[str, int, str], dict[str, float | int]] = {}
        def grain(date, account, model):
            return grains.setdefault((date, account, model), {
                "input": 0, "cache": 0, "output": 0, "requests": 0, "cost": 0.0})
        for row in old.execute("SELECT * FROM token_usage"):
            bucket = grain(row["date"], row["account_id"], row["model"])
            if row["token_type"] == "output":
                bucket["output"] += row["amount"]
            elif row["token_type"] == "input_cache_hit":
                bucket["input"] += row["amount"]
                bucket["cache"] += row["amount"]
            elif row["token_type"] == "input_cache_miss":
                bucket["input"] += row["amount"]
        for row in old.execute("SELECT * FROM request_usage"):
            grain(row["date"], row["account_id"], row["model"])["requests"] += row["count"]
        for row in old.execute("SELECT * FROM cost_entry"):
            grain(row["date"], row["account_id"], row["model"])["cost"] += row["cost"]
        types = proxy_mapping["account_types"]
        for (date, account, model), bucket in grains.items():
            billed = bucket["cost"] if types.get(account, "api") == "api" else 0.0
            new.execute(upsert, (
                date, account, model, bucket["input"], bucket["cache"], bucket["output"],
                bucket["requests"], bucket["cost"], billed))

        local_by_mask = {}
        for row in proxy.execute("SELECT id,account_id,key_value FROM upstream_keys"):
            local_by_mask[(row["account_id"], mask_key(row["key_value"]))] = (
                proxy_mapping["credential_map"].get(row["id"]))
        for row in old.execute("SELECT * FROM proxy_plan_summary"):
            unit = local_by_mask.get((row["account_id"], row["key_masked"])) or stable_uuid(
                "credential", row["account_id"], row["key_masked"])
            new.execute(
                "INSERT INTO monthly_recurring_costs(month,account_id,billing_unit_id,"
                "recurring_charge,equivalent_cost) VALUES(?,?,?,?,?)",
                (row["month"], row["account_id"], unit, row["subscription_cost"],
                 row["virtual_cost"]),
            )
        new.commit()
    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        proxy.close()
        new.close()


def write_manifest(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def backup_files(proxy: Path, dashboard: Path, backup_dir: Path) -> list[dict]:
    backup_dir.mkdir(parents=True, exist_ok=False)
    result = []
    candidates = [proxy, dashboard]
    for base in (proxy, dashboard):
        candidates.extend(Path(str(base) + suffix) for suffix in ("-wal", "-shm", ".migrate.lock"))
    candidates.append(Path(str(proxy) + ".request-log.spool"))
    candidates.extend((proxy.parent / name) for name in (
        "sync_config.json", "config_snapshot.json"))
    for source in candidates:
        if not source.exists() or not source.is_file():
            continue
        destination = backup_dir / source.name
        shutil.copy2(source, destination)
        result.append({"source": str(source), "backup": str(destination),
                       "sha256": checksum(destination), "size": destination.stat().st_size})
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
    manifest["stage"] = f"{label}_replaced"
    write_manifest(manifest_path, manifest)


def validate_backups(manifest: dict) -> None:
    for item in manifest.get("backups", []):
        path = Path(item["backup"])
        if not path.is_file() or checksum(path) != item["sha256"]:
            raise RuntimeError(f"backup missing or checksum changed: {path}")


def rollback_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_backups(manifest)
    proxy = Path(manifest["proxy_db"]); dashboard = Path(manifest["dashboard_db"])
    with migration_locks(proxy, dashboard):
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        for item in manifest["backups"]:
            source = Path(item["source"])
            if source not in {proxy, dashboard}:
                continue
            staged = source.with_name(source.name + ".v0-restore")
            shutil.copy2(item["backup"], staged)
            if checksum(staged) != item["sha256"]:
                raise RuntimeError(f"restore checksum failed: {source}")
            os.replace(staged, source)
    manifest["stage"] = "rolled_back"
    write_manifest(path, manifest)


def resume_manifest(path: Path) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_backups(manifest)
    proxy = Path(manifest["proxy_db"])
    dashboard = Path(manifest["dashboard_db"])
    proxy_shadow = Path(manifest["shadows"]["proxy"])
    dashboard_shadow = Path(manifest["shadows"]["dashboard"])
    stage = manifest["stage"]
    with migration_locks(proxy, dashboard):
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        if stage in {"verified", "dry_run_complete"}:
            atomic_replace(proxy, proxy_shadow, path, manifest, "proxy")
            stage = manifest["stage"]
        if stage == "proxy_replaced":
            atomic_replace(dashboard, dashboard_shadow, path, manifest, "dashboard")
            stage = manifest["stage"]
        if stage == "dashboard_replaced":
            manifest["stage"] = "complete"
            write_manifest(path, manifest)
        elif stage != "complete":
            raise RuntimeError(f"manifest stage cannot be resumed automatically: {stage}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-db", default="data/proxy.db")
    parser.add_argument("--dashboard-db", default="data/dashboard.db")
    parser.add_argument("--schema-dir", default=str(REPO / "schema"))
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--apply", action="store_true",
                        help="atomically replace source databases after verification")
    parser.add_argument("--resume-manifest")
    parser.add_argument("--rollback-manifest")
    args = parser.parse_args()
    if args.resume_manifest and args.rollback_manifest:
        parser.error("choose only one of --resume-manifest/--rollback-manifest")
    if args.resume_manifest:
        resume_manifest(Path(args.resume_manifest).resolve())
        return
    if args.rollback_manifest:
        rollback_manifest(Path(args.rollback_manifest).resolve())
        return
    proxy = Path(args.proxy_db).resolve()
    dashboard = Path(args.dashboard_db).resolve()
    schema_root = Path(args.schema_dir).resolve()
    if not proxy.is_file() or not dashboard.is_file():
        parser.error("both V0 database files must exist")
    source_tz = ZoneInfo(args.timezone)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = proxy.parent / f"v0-to-v1-{stamp}.manifest.json"
    backup_dir = proxy.parent / f"v0-to-v1-{stamp}.backup"
    manifest = {"transition": "0-to-1", "stage": "started",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "timezone": args.timezone, "apply": args.apply,
                "proxy_db": str(proxy), "dashboard_db": str(dashboard),
                "shadows": {"proxy": str(shadow_path(proxy)),
                            "dashboard": str(shadow_path(dashboard))}}

    with migration_locks(proxy, dashboard):
        source_version(proxy, 19)
        source_version(dashboard, 6)
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        assert_spool_empty(proxy)
        manifest["backups"] = backup_files(proxy, dashboard, backup_dir)
        manifest["stage"] = "backed_up"
        write_manifest(manifest_path, manifest)

        proxy_shadow = prepare_shadow(proxy, "proxy", schema_root)
        dashboard_shadow = prepare_shadow(dashboard, "dashboard", schema_root)
        manifest["stage"] = "shadows_created"
        write_manifest(manifest_path, manifest)

        mapping = transform_proxy(proxy, proxy_shadow, source_tz)
        transform_dashboard(dashboard, dashboard_shadow, proxy, mapping)
        manifest["stage"] = "transformed"
        write_manifest(manifest_path, manifest)
        manifest["verification"] = {
            "proxy": verify_proxy(str(proxy), str(proxy_shadow)),
            "dashboard": verify_dashboard(str(dashboard), str(dashboard_shadow)),
        }
        manifest["stage"] = "verified"
        write_manifest(manifest_path, manifest)

        if args.apply:
            atomic_replace(proxy, proxy_shadow, manifest_path, manifest, "proxy")
            atomic_replace(dashboard, dashboard_shadow, manifest_path, manifest, "dashboard")
            manifest["stage"] = "complete"
        else:
            manifest["stage"] = "dry_run_complete"
        write_manifest(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), "stage": manifest["stage"],
                      "verification": manifest.get("verification")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
