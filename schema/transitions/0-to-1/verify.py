#!/usr/bin/env python3
"""Independent V0→V1 shadow-database verification."""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
from pathlib import Path


class VerificationError(RuntimeError):
    pass


def latest_user_version(schema_root: Path, database: str) -> int:
    versions = []
    for path in (schema_root / database / "v1").glob("*.sql"):
        match = re.match(r"^(\d+)-(\d+)_", path.name)
        if match:
            versions.append(int(match.group(1)) * 10_000 + int(match.group(2)))
    if not versions:
        raise VerificationError(f"no V1 schema files for {database}")
    return max(versions)


def scalar(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchone()[0]


def verify_proxy(v0_path: str, v1_path: str, expected_version: int | None = None,
                 spool_records: list[dict] | None = None) -> dict[str, int | float]:
    old = sqlite3.connect(v0_path)
    new = sqlite3.connect(v1_path)
    try:
        expected_accounts = scalar(
            old, "SELECT count(*) FROM upstream_accounts WHERE is_aggregate=0")
        actual_accounts = scalar(
            new, "SELECT count(*) FROM accounts WHERE name!='legacy-unattributed'")
        if actual_accounts != expected_accounts:
            raise VerificationError(
                f"proxy accounts: expected {expected_accounts}, got {actual_accounts}")
        spool_records = spool_records or []
        old_events = {row[0] for row in old.execute(
            "SELECT event_id FROM request_log")}
        imported = [row for row in spool_records
                    if row.get("event_id") not in old_events]
        for table in ("request_log", "request_attempts"):
            before = scalar(old, f"SELECT count(*) FROM {table}")
            after = scalar(new, f"SELECT count(*) FROM {table}")
            expected = before + (len(imported) if table == "request_log" else
                                 sum(len(row.get("attempts", [])) for row in imported))
            if expected != after:
                raise VerificationError(f"{table}: expected {before}, got {after}")
        token_before = scalar(old, "SELECT COALESCE(sum(total_tokens),0) FROM request_log")
        token_after = scalar(new, "SELECT COALESCE(sum(total_tokens),0) FROM request_log")
        cost_before = float(scalar(old, "SELECT COALESCE(sum(api_cost),0) FROM request_log"))
        cost_after = float(scalar(new, "SELECT COALESCE(sum(equivalent_cost),0) FROM request_log"))
        spool_tokens = sum(int(row.get("total_tokens", 0) or 0) for row in imported)
        spool_cost = sum(float(row.get("cost", 0.0) or 0.0) for row in imported)
        if token_before + spool_tokens != token_after or not math.isclose(
                cost_before + spool_cost, cost_after, rel_tol=1e-10, abs_tol=1e-8):
            raise VerificationError("proxy token/cost totals changed")
        disabled_with_secret = scalar(
            new, "SELECT count(*) FROM upstream_credentials c WHERE EXISTS("
                 "SELECT 1 FROM upstream_secrets s WHERE s.credential_uuid=c.uuid)"
                 " AND c.disabled_at IS NOT NULL")
        if disabled_with_secret:
            raise VerificationError(
                f"migration changed local credential state: "
                f"{disabled_with_secret} secret-bearing credential(s) disabled")
        fk = new.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise VerificationError(f"proxy foreign_key_check failed: {fk[:10]}")
        version = scalar(new, "PRAGMA user_version")
        if expected_version is not None and version != expected_version:
            raise VerificationError(f"proxy user_version is {version}, expected {expected_version}")
        return {"accounts": actual_accounts, "requests": scalar(new, "SELECT count(*) FROM request_log"),
                "attempts": scalar(new, "SELECT count(*) FROM request_attempts"),
                "tokens": token_after, "equivalent_cost": cost_after}
    finally:
        old.close()
        new.close()


def verify_dashboard(v0_path: str, v1_path: str, expected_version: int | None = None) -> dict[str, int | float]:
    old = sqlite3.connect(v0_path)
    new = sqlite3.connect(v1_path)
    try:
        input_before = scalar(
            old, "SELECT COALESCE(sum(amount),0) FROM token_usage "
                 "WHERE token_type IN ('input_cache_hit','input_cache_miss')")
        cache_before = scalar(
            old, "SELECT COALESCE(sum(amount),0) FROM token_usage "
                 "WHERE token_type='input_cache_hit'")
        output_before = scalar(
            old, "SELECT COALESCE(sum(amount),0) FROM token_usage "
                 "WHERE token_type='output'")
        requests_before = scalar(old, "SELECT COALESCE(sum(count),0) FROM request_usage")
        cost_before = float(scalar(old, "SELECT COALESCE(sum(cost),0) FROM cost_entry"))
        values = new.execute(
            "SELECT COALESCE(sum(input_tokens),0),COALESCE(sum(cache_tokens),0),"
            "COALESCE(sum(output_tokens),0),COALESCE(sum(request_count),0),"
            "COALESCE(sum(equivalent_cost),0) FROM daily_usage").fetchone()
        expected = (input_before, cache_before, output_before, requests_before)
        if tuple(values[:4]) != expected or not math.isclose(
                float(values[4]), cost_before, rel_tol=1e-10, abs_tol=1e-8):
            raise VerificationError(
                f"dashboard totals changed: expected {expected + (cost_before,)}, got {values}")
        recurring_before = float(scalar(
            old, "SELECT COALESCE(sum(subscription_cost),0) FROM proxy_plan_summary"))
        recurring_after = float(scalar(
            new, "SELECT COALESCE(sum(recurring_charge),0) FROM monthly_recurring_costs"))
        if not math.isclose(recurring_before, recurring_after,
                            rel_tol=1e-10, abs_tol=1e-8):
            raise VerificationError("dashboard recurring charge total changed")
        fk = new.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise VerificationError(f"dashboard foreign_key_check failed: {fk[:10]}")
        version = scalar(new, "PRAGMA user_version")
        if expected_version is not None and version != expected_version:
            raise VerificationError(f"dashboard user_version is {version}, expected {expected_version}")
        return {"daily_rows": scalar(new, "SELECT count(*) FROM daily_usage"),
                "requests": values[3], "equivalent_cost": float(values[4]),
                "recurring_charge": recurring_after}
    finally:
        old.close()
        new.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-v0", required=True)
    parser.add_argument("--proxy-v1", required=True)
    parser.add_argument("--dashboard-v0", required=True)
    parser.add_argument("--dashboard-v1", required=True)
    parser.add_argument("--schema-dir", default=str(Path(__file__).resolve().parents[2] / "schema"))
    args = parser.parse_args()
    for path in vars(args).values():
        if not Path(path).is_file():
            parser.error(f"database not found: {path}")
    root = Path(args.schema_dir).resolve()
    print({"proxy": verify_proxy(args.proxy_v0, args.proxy_v1,
                                  latest_user_version(root, "proxy")),
           "dashboard": verify_dashboard(args.dashboard_v0, args.dashboard_v1,
                                           latest_user_version(root, "dashboard"))})


if __name__ == "__main__":
    main()
