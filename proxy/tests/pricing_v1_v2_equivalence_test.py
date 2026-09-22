#!/usr/bin/env python3
"""Current pricing and V1 -> V2 recovery gates.

The old pricing gate compared two historical V0 trigger revisions.  That did
not exercise the schema which the proxy runs today.  These checks build the
published V1 tip, clone the same priced configuration, and compare new usage
events against a V2-upgraded copy.  The recovery case deliberately stops at
V2.0, then resumes through V2.4 and repeats the upgrade to prove the durable
schema runner is restart-safe without rewriting frozen usage facts.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from support import sqlite_connection


CASES = (
    ("exact", "gpt-4o", 1_000, 200, 500, 0.0184),
    ("wildcard", "gpt-4o-mini", 100, 20, 50, 0.00024),
    ("tier", "tier-model", 1_500, 100, 300, 0.0161),
    ("usd-fx", "usd-model", 1_000, 0, 0, 0.0216),
    ("no-match", "unpriced-model", 100, 0, 50, 0.0),
)


def _migrate_v1(db_path: Path, schema_root: Path) -> None:
    from app.db.migrations import migrate

    migrate(str(db_path), str(schema_root / "token-board" / "v1"), "token-board")


def _upgrade(db_path: Path, schema_root: Path, minor: int) -> None:
    from app.db.migrations import SchemaVersion, apply_sql_migrations

    apply_sql_migrations(
        str(db_path), str(schema_root), "token-board", SchemaVersion(2, minor)
    )


def _clone_database(source: Path, destination: Path) -> None:
    """Clone through SQLite so WAL-resident rows are included."""
    with sqlite_connection(source) as src, sqlite_connection(destination) as dst:
        src.backup(dst)


def _seed_current_pricing(db_path: Path) -> None:
    """Seed only current V1 tables, using the public pricing shape."""
    today = datetime.now(timezone.utc).date().isoformat()
    with sqlite_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO accounts(uuid,name,account_kind,valid_from) "
            "VALUES('pricing-account','pricing-account','proxy',?)",
            (f"{today}T00:00:00Z",),
        )
        account_id = conn.execute(
            "SELECT id FROM accounts WHERE uuid='pricing-account'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,"
            "valid_from) VALUES('pricing-contract',?,'metered','account',?)",
            (account_id, f"{today}T00:00:00Z"),
        )
        conn.executemany(
            "INSERT INTO pricing_rules"
            "(id,model_pattern,priority,input_price,cache_read_price,output_price,currency)"
            "VALUES(?,?,?,?,?,?,?)",
            [
                (1, "gpt-4o", 0, 10.0, 2.0, 20.0, "CNY"),
                (2, "gpt-*", 10, 1.0, 0.5, 3.0, "CNY"),
                (3, "tier-model", 0, 10.0, 2.0, 20.0, "CNY"),
                (4, "usd-model", 0, 3.0, 1.0, 6.0, "USD"),
            ],
        )
        conn.execute(
            "INSERT INTO pricing_length_tiers"
            "(pricing_rule_id,threshold_tokens,input_price,cache_read_price,output_price)"
            "VALUES(3,1000,8.0,1.0,16.0)"
        )
        conn.execute(
            "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
            "VALUES('USD','CNY',?,7.2)",
            (today,),
        )
        conn.commit()


def _insert_cases(db_path: Path, prefix: str) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    with sqlite_connection(db_path) as conn:
        for index, (label, model, prompt, cache, completion, _expected) in enumerate(
            CASES, start=1
        ):
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,source_kind,account_id,model,prompt_tokens,"
                "completion_tokens,cache_read_tokens,total_tokens,status_code,"
                "attempt_count,requested_at,pricing_status,equivalent_cost,"
                "billed_usage_cost) VALUES(?,?,?,?,?,?,?,?,200,1,?,'pending',0,0)",
                (
                    f"{prefix}-{label}",
                    "proxy",
                    1,
                    model,
                    prompt,
                    completion,
                    cache,
                    prompt + completion,
                    f"{today}T12:00:00Z",
                ),
            )
        conn.commit()


def _usage_rows(db_path: Path) -> list[tuple[object, ...]]:
    with sqlite_connection(db_path) as conn:
        return conn.execute(
            "SELECT event_id,model,prompt_tokens,cache_read_tokens,"
            "completion_tokens,pricing_status,equivalent_cost,billed_usage_cost "
            "FROM request_log ORDER BY id"
        ).fetchall()


def _assert_expected(rows: list[tuple[object, ...]]) -> None:
    assert len(rows) == len(CASES), f"expected {len(CASES)} rows, got {len(rows)}"
    for row, case in zip(rows, CASES):
        label, _model, _prompt, _cache, _completion, expected = case
        status = row[5]
        cost = float(row[6])
        if label == "no-match":
            assert status == "unrated", (label, row)
            assert cost == 0.0, (label, row)
        else:
            assert status == "rated", (label, row)
            assert abs(cost - expected) < 1e-12, (label, cost, expected, row)
            assert abs(float(row[7]) - expected) < 1e-12, (label, row)


def run_equivalence(schema_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pricing-v1-v2-") as directory:
        root = Path(directory)
        v1 = root / "v1.db"
        v2 = root / "v2.db"
        _migrate_v1(v1, schema_root)
        _seed_current_pricing(v1)
        _clone_database(v1, v2)
        _upgrade(v2, schema_root, 4)

        _insert_cases(v1, "v1")
        _insert_cases(v2, "v2")
        rows_v1 = _usage_rows(v1)
        rows_v2 = _usage_rows(v2)
        _assert_expected(rows_v1)
        _assert_expected(rows_v2)
        assert [row[1:] for row in rows_v1] == [row[1:] for row in rows_v2], (
            "V1 and V2 pricing diverged:\n"
            f"V1={rows_v1}\nV2={rows_v2}"
        )
    print(f"V1/V2 pricing equivalence passed across {len(CASES)} current cases")


def run_recovery(schema_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pricing-v1-v2-recovery-") as directory:
        db_path = Path(directory) / "token-board.db"
        _migrate_v1(db_path, schema_root)
        _seed_current_pricing(db_path)
        _insert_cases(db_path, "recovery")
        before = _usage_rows(db_path)
        _assert_expected(before)

        # A process may be stopped after the first V2 file has committed.  A
        # later process must continue at the next file, not rebuild facts.
        _upgrade(db_path, schema_root, 0)
        assert _usage_rows(db_path) == before
        _upgrade(db_path, schema_root, 4)
        assert _usage_rows(db_path) == before

        # Re-running the completed upgrade is the restart path used after a
        # health-check timeout or a process replacement.
        _upgrade(db_path, schema_root, 4)
        assert _usage_rows(db_path) == before
        with sqlite_connection(db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 20004
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE major=2"
            ).fetchone()[0] == 5
    print("V1/V2 pricing recovery passed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("equivalence", "recovery"))
    parser.add_argument("schema_root", type=Path)
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))
    if args.mode == "equivalence":
        run_equivalence(args.schema_root.resolve())
    else:
        run_recovery(args.schema_root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
