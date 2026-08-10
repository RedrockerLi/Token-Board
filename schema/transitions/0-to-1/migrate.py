#!/usr/bin/env python3
"""Offline, resumable Token Board V0→V1 shadow-database transition."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))

from transition_common import (  # noqa: E402
    LEGACY_CREDENTIAL_UUID, assert_offline_and_checkpoint,
    checksum, mask_key, migration_locks, prepare_shadow, read_usage_spool,
    shadow_path, source_version, stable_uuid, utc_timestamp,
)
from transition_runtime import (  # noqa: E402
    atomic_replace, backup_files, rebuild_config_snapshot,
    rollback_manifest, write_manifest,
)
from transition_resume import resume_transition  # noqa: E402
from spool_transform import append_spool_attempts, append_spool_requests  # noqa: E402
from verify import verify_dashboard, verify_proxy  # noqa: E402


def transform_proxy(source: Path, shadow: Path, source_tz: ZoneInfo,
                    spool_records: list[dict] | None = None) -> dict:
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
                 utc_timestamp(row["valid_from"], source_tz),
                 utc_timestamp(row["deleted_at"], source_tz),
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
        # A V0 mask is a display identity, not a credential identity.  The
        # old table can retain two historical rows with the same mask after a
        # rotation/deletion.  Include the stable V0 key id so those rows keep
        # distinct UUIDs instead of aborting or overwriting each other.
        local_masks: set[tuple[int, str]] = set()
        credential_map: dict[int, str] = {}
        for row in local_keys:
            masked = mask_key(row["key_value"])
            identity = (row["account_id"], masked)
            local_masks.add(identity)
            credential_uuid = stable_uuid(
                "credential", row["account_id"], row["id"], masked)
            credential_map[row["id"]] = credential_uuid
            new.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,"
                "valid_from,created_at,deleted_at) VALUES(?,?,?,?,?,?,?,?)",
                (credential_uuid, row["id"], row["account_id"], row["position"], masked,
                 utc_timestamp(row["valid_from"], source_tz),
                 utc_timestamp(row["created_at"], source_tz),
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
        cloud_runtime_id = 1_000_000_000
        for row in old.execute("SELECT * FROM upstream_keys_cloud ORDER BY account_id,position"):
            identity = (row["account_id"], row["key_masked"])
            if identity in local_masks:
                continue
            credential_uuid = stable_uuid(
                "credential", row["account_id"], "cloud", row["position"],
                row["key_masked"])
            new.execute(
                "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,position,key_masked,"
                "valid_from,created_at,deleted_at) VALUES(?,?,?,?,?,?,?,?)",
                (credential_uuid, cloud_runtime_id, row["account_id"],
                 row["position"], row["key_masked"],
                 utc_timestamp(row["valid_from"], source_tz),
                 "1970-01-01T00:00:00Z",
                 utc_timestamp(row["deleted_at"], source_tz)),
            )
            cloud_runtime_id += 1
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
                 cancellation, utc_timestamp(row["valid_from"], source_tz)
                 or "1970-01-01",
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

        imported_spool_requests = append_spool_requests(
            old, new, spool_records, credential_map, route_ids, contract_ids,
            legacy_account_id, source_tz)
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
        append_spool_attempts(new, imported_spool_requests, credential_map,
                              routable_ids, legacy_upstream_id)
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


def transform_dashboard(source: Path, shadow: Path, proxy_source: Path | None,
                        proxy_mapping: dict, credential_mask_lookup=None) -> None:
    old = sqlite3.connect(source)
    old.row_factory = sqlite3.Row
    proxy = sqlite3.connect(proxy_source) if proxy_source else None
    if proxy is not None: proxy.row_factory = sqlite3.Row
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

        local_by_mask = dict(credential_mask_lookup or {})
        if proxy is not None:
            has_legacy_keys = proxy.execute("SELECT 1 FROM sqlite_master WHERE "
                                            "type='table' AND name='upstream_keys'").fetchone()
            if has_legacy_keys:
                for row in proxy.execute("SELECT id,account_id,key_value FROM upstream_keys"):
                    local_by_mask[(row["account_id"], mask_key(row["key_value"]))] = (
                        proxy_mapping["credential_map"].get(row["id"]))
        for row in old.execute("SELECT * FROM proxy_plan_summary"):
            unit = local_by_mask.get((row["account_id"], row["key_masked"])) or stable_uuid(
                "credential", row["account_id"], row["key_masked"])
            new.execute(
                "INSERT INTO monthly_recurring_costs(month,account_id,billing_unit_id,"
                "recurring_charge,equivalent_cost,normalized_recurring_cost,"
                "base_currency) VALUES(?,?,?,?,?,?,?)",
                (row["month"], row["account_id"], unit, row["subscription_cost"],
                 row["virtual_cost"], row["subscription_cost"], "CNY"),
            )
        new.commit()
    except Exception:
        new.rollback()
        raise
    finally:
        old.close()
        if proxy is not None:
            proxy.close()
        new.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-db", default="data/proxy.db")
    parser.add_argument("--dashboard-db", default="data/dashboard.db")
    parser.add_argument("--schema-dir", default=str(REPO / "schema"))
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--confirm-timezone",
                        help="required with --apply; must exactly match --timezone")
    parser.add_argument("--apply", action="store_true",
                        help="atomically replace source databases after verification")
    parser.add_argument("--resume-manifest")
    parser.add_argument("--rollback-manifest")
    parser.add_argument(
        "--inject-failure",
        choices=("backed_up", "shadows_created", "transformed", "verified",
                 "proxy_replaced", "dashboard_replaced", "snapshot_rebuilt"),
        help="test-only interruption after the named manifest stage")
    args = parser.parse_args()
    if args.apply and args.confirm_timezone != args.timezone:
        parser.error("--apply requires --confirm-timezone matching --timezone")
    if args.resume_manifest and args.rollback_manifest:
        parser.error("choose only one of --resume-manifest/--rollback-manifest")
    if args.resume_manifest:
        resume_transition(Path(args.resume_manifest).resolve(),
                          transform_proxy, transform_dashboard)
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
    def inject(stage: str) -> None:
        if args.inject_failure == stage:
            raise RuntimeError(f"injected transition failure at stage={stage}")
    # Include microseconds so a rollback followed immediately by a second
    # transition cannot reuse the previous manifest/backup directory.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    manifest_path = proxy.parent / f"v0-to-v1-{stamp}.manifest.json"
    backup_dir = proxy.parent / f"v0-to-v1-{stamp}.backup"
    manifest = {"transition": "0-to-1", "stage": "started",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "timezone": args.timezone, "apply": args.apply,
                "proxy_db": str(proxy), "dashboard_db": str(dashboard),
                "schema_dir": str(schema_root),
                "shadows": {"proxy": str(shadow_path(proxy)),
                            "dashboard": str(shadow_path(dashboard))},
                "remote_v0_policy": "retain-read-only; publish V1 only after all nodes transition"}

    with migration_locks(proxy, dashboard):
        source_version(proxy, 19)
        source_version(dashboard, 6)
        assert_offline_and_checkpoint(proxy)
        assert_offline_and_checkpoint(dashboard)
        spool_records = read_usage_spool(proxy)
        manifest["spool"] = {
            "path": str(Path(str(proxy) + ".request-log.spool")),
            "records": len(spool_records),
        }
        sample_conn = sqlite3.connect(proxy)
        try:
            samples = sample_conn.execute(
                "SELECT created_at FROM upstream_accounts WHERE created_at IS NOT NULL "
                "ORDER BY created_at LIMIT 3"
            ).fetchall()
            manifest["timezone_samples"] = [
                {"source": row[0], "utc": utc_timestamp(row[0], source_tz)}
                for row in samples
            ]
        finally:
            sample_conn.close()
        manifest["backups"] = backup_files(proxy, dashboard, backup_dir)
        manifest["stage"] = "backed_up"; write_manifest(manifest_path, manifest)
        inject("backed_up")
        proxy_shadow = prepare_shadow(proxy, "proxy", schema_root)
        dashboard_shadow = prepare_shadow(dashboard, "dashboard", schema_root)
        manifest["stage"] = "shadows_created"; write_manifest(manifest_path, manifest)
        inject("shadows_created")
        mapping = transform_proxy(proxy, proxy_shadow, source_tz, spool_records)
        transform_dashboard(dashboard, dashboard_shadow, proxy, mapping)
        manifest["stage"] = "transformed"; write_manifest(manifest_path, manifest)
        inject("transformed")
        manifest["verification"] = {
            "proxy": verify_proxy(str(proxy), str(proxy_shadow),
                                   spool_records=spool_records),
            "dashboard": verify_dashboard(str(dashboard), str(dashboard_shadow)),
        }
        manifest["stage"] = "verified"; write_manifest(manifest_path, manifest)
        inject("verified")

        if args.apply:
            atomic_replace(proxy, proxy_shadow, manifest_path, manifest, "proxy")
            inject("proxy_replaced")
            atomic_replace(dashboard, dashboard_shadow, manifest_path, manifest, "dashboard")
            inject("dashboard_replaced")
            manifest["config_snapshot"] = str(rebuild_config_snapshot(proxy))
            write_manifest(manifest_path, manifest)
            inject("snapshot_rebuilt")
            manifest["stage"] = "complete"
        else:
            manifest["stage"] = "dry_run_complete"
        write_manifest(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), "stage": manifest["stage"],
                      "verification": manifest.get("verification")},
                     ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
