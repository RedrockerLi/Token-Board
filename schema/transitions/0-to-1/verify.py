#!/usr/bin/env python3
"""Independent V0→V1 shadow-database verification."""

from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path


class VerificationError(RuntimeError):
    pass


def scalar(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchone()[0]


def verify_proxy(v0_path: str, v1_path: str) -> dict[str, int | float]:
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
        for table in ("request_log", "request_attempts"):
            before = scalar(old, f"SELECT count(*) FROM {table}")
            after = scalar(new, f"SELECT count(*) FROM {table}")
            if before != after:
                raise VerificationError(f"{table}: expected {before}, got {after}")
        token_before = scalar(old, "SELECT COALESCE(sum(total_tokens),0) FROM request_log")
        token_after = scalar(new, "SELECT COALESCE(sum(total_tokens),0) FROM request_log")
        cost_before = float(scalar(old, "SELECT COALESCE(sum(api_cost),0) FROM request_log"))
        cost_after = float(scalar(new, "SELECT COALESCE(sum(equivalent_cost),0) FROM request_log"))
        if token_before != token_after or not math.isclose(
                cost_before, cost_after, rel_tol=1e-10, abs_tol=1e-8):
            raise VerificationError("proxy token/cost totals changed")
        fk = new.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise VerificationError(f"proxy foreign_key_check failed: {fk[:10]}")
        version = scalar(new, "PRAGMA user_version")
        if version != 10000:
            raise VerificationError(f"proxy user_version is {version}, expected 10000")
        return {"accounts": actual_accounts, "requests": scalar(new, "SELECT count(*) FROM request_log"),
                "attempts": scalar(new, "SELECT count(*) FROM request_attempts"),
                "tokens": token_after, "equivalent_cost": cost_after}
    finally:
        old.close()
        new.close()


def verify_dashboard(v0_path: str, v1_path: str) -> dict[str, int | float]:
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
        if scalar(new, "PRAGMA user_version") != 10000:
            raise VerificationError("dashboard is not V1.0")
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
    args = parser.parse_args()
    for path in vars(args).values():
        if not Path(path).is_file():
            parser.error(f"database not found: {path}")
    print({"proxy": verify_proxy(args.proxy_v0, args.proxy_v1),
           "dashboard": verify_dashboard(args.dashboard_v0, args.dashboard_v1)})


if __name__ == "__main__":
    main()
