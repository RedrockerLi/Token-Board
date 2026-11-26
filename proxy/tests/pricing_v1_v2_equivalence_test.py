#!/usr/bin/env python3
"""Current pricing and V1 -> V2 recovery gates.

The old pricing gate compared two historical V0 trigger revisions.  That did
not exercise the schema which the proxy runs today.  These checks build the
published V1.14 pricing baseline and the V1 tip, clone the same priced
configuration, and compare new usage events against V2 upgrades.  The current
V2 tip must reproduce the V1.14 answers: the slot multiplier is selected by the
wall-clock minute and the FX filter is applied inside the cost subquery.  The
V2.9 trigger definition stays frozen for the rows it already priced, so the
snapshot check also proves that replacing the trigger rewrites no history.  The
recovery case deliberately stops at V2.0, then resumes through V2.7 and repeats
the upgrade to prove the durable schema runner is restart-safe without
rewriting frozen usage facts.
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
    # 1M prompt tokens cost 5.0 before the multiplier of the slot covering the
    # insert minute, so the write-time answer is 10.0.
    ("slot", "slot-model", 1_000_000, 0, 0, 10.0),
    # The cheaper EUR rule has no EUR->CNY rate.  Only the FX filter inside the
    # cost subquery skips it and reaches the 20.0/M CNY rule.
    ("fx-race", "fx-race", 1_000_000, 0, 0, 20.0),
)

# V2.9 re-created price_usage_event without the minute window and without the
# FX filter.  A database priced under that definition keeps these answers when
# the current trigger replaces it: history is never re-priced.
V29_FROZEN = {"slot": (5.0, "rated"), "fx-race": (0.0, "rated")}

SLOT_RULE_ID = 5
SLOT_WINDOW_OFFSET = 60


def _migrate_v1(db_path: Path, schema_root: Path, target=None) -> None:
    from app.db.migrations import apply_sql_migrations

    apply_sql_migrations(str(db_path), str(schema_root), "token-board", target)


def _upgrade(db_path: Path, schema_root: Path, minor: int) -> None:
    from app.db.migrations import SchemaVersion, apply_sql_migrations

    apply_sql_migrations(
        str(db_path), str(schema_root), "token-board", SchemaVersion(2, minor)
    )


def _latest_v2_minor(schema_root: Path) -> int:
    from app.db.migrations import _sql_steps

    return max(step.version.minor for step in _sql_steps(schema_root / "token-board" / "v2"))


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
                (SLOT_RULE_ID, "slot-model", 0, 5.0, 0.0, 0.0, "CNY"),
                (6, "fx-race", 0, 1.0, 0.0, 0.0, "EUR"),
                (7, "fx-race", 10, 20.0, 0.0, 0.0, "CNY"),
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


def _current_utc_minute() -> int:
    now = datetime.now(timezone.utc)
    return now.hour * 60 + now.minute


def _seed_slot_window(db_path: Path) -> None:
    """Add the two slot rows against the minute that is current right now.

    The lower id never covers the insert minute while the higher id covers it,
    so the V2.9 trigger (lowest slot id) and the V1.14 / current V2 trigger
    (wall-clock minute) must disagree.  The minute is read immediately before
    the insert so the fixture cannot straddle a minute boundary while the
    databases are built.
    """
    minute = _current_utc_minute()
    outside = (minute + SLOT_WINDOW_OFFSET) % 1440
    end = min(minute + 1, 1440)
    with sqlite_connection(db_path) as conn:
        conn.executemany(
            "INSERT INTO pricing_slots"
            "(id,pricing_rule_id,start_minute,end_minute,multiplier)"
            "VALUES(?,?,?,?,?)",
            (
                (1, SLOT_RULE_ID, outside, outside + 1, 1.0),
                (2, SLOT_RULE_ID, minute, end, 2.0),
            ),
        )
        conn.commit()
    print(
        f"pricing slots: 2.0 on UTC minute [{minute},{end}), "
        f"1.0 on [{outside},{outside + 1})"
    )


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


def _assert_expected(rows: list[tuple[object, ...]], frozen=None) -> None:
    """Assert the write-time answer for every case.

    ``frozen`` overrides the answer of individual labels for a database that
    was priced by an older trigger definition.
    """
    overrides = frozen or {}
    assert len(rows) == len(CASES), f"expected {len(CASES)} rows, got {len(rows)}"
    for row, case in zip(rows, CASES):
        label, _model, _prompt, _cache, _completion, expected = case
        expected_status = "unrated" if label == "no-match" else "rated"
        if label in overrides:
            expected, expected_status = overrides[label]
        status = row[5]
        cost = float(row[6])
        if expected_status == "unrated":
            assert status == "unrated", (label, row)
            assert cost == 0.0, (label, row)
        else:
            assert status == "rated", (label, row)
            assert abs(cost - expected) < 1e-12, (label, cost, expected, row)
            assert abs(float(row[7]) - expected) < 1e-12, (label, row)


def run_equivalence(schema_root: Path) -> None:
    from app.db.migrations import SchemaVersion

    latest = _latest_v2_minor(schema_root)
    assert latest >= 9, f"V2 tip V2.{latest} predates the V2.9 snapshot"
    with tempfile.TemporaryDirectory(prefix="pricing-v1-v2-") as directory:
        root = Path(directory)
        baseline = root / "v1-14.db"
        source = root / "v1-tip.db"
        current = root / "v2-latest.db"
        frozen = root / "v2-9.db"

        # V1.14 is the last published price_usage_event with the wall-clock
        # slot window and the FX filter inside the cost subquery.
        _migrate_v1(baseline, schema_root, SchemaVersion(1, 14))
        _seed_current_pricing(baseline)
        _seed_slot_window(baseline)
        _insert_cases(baseline, "v114")

        # V2 is a transformation of the V1 tip, so both V2 copies start from
        # the same seeded tip database.
        _migrate_v1(source, schema_root)
        _seed_current_pricing(source)
        _clone_database(source, current)
        _clone_database(source, frozen)
        _upgrade(current, schema_root, latest)
        _upgrade(frozen, schema_root, 9)
        for db_path, prefix in ((current, "v2"), (frozen, "v29")):
            _seed_slot_window(db_path)
            _insert_cases(db_path, prefix)

        rows_baseline = _usage_rows(baseline)
        rows_current = _usage_rows(current)
        rows_frozen = _usage_rows(frozen)
        _assert_expected(rows_baseline)
        _assert_expected(rows_current)
        _assert_expected(rows_frozen, frozen=V29_FROZEN)
        assert [row[1:] for row in rows_baseline] == [
            row[1:] for row in rows_current
        ], (
            "V1.14 and the current V2 tip priced the same events differently:\n"
            f"V1.14={rows_baseline}\nV2.{latest}={rows_current}"
        )

        # Replacing the V2.9 trigger must not re-price rows it already priced.
        _upgrade(frozen, schema_root, latest)
        assert _usage_rows(frozen) == rows_frozen, (
            "upgrading a V2.9 database rewrote priced request_log rows"
        )
    print(
        f"V1.14/V2.{latest} pricing equivalence passed across {len(CASES)} "
        "current cases; the V2.9 frozen snapshot kept its amounts"
    )


def run_recovery(schema_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pricing-v1-v2-recovery-") as directory:
        db_path = Path(directory) / "token-board.db"
        _migrate_v1(db_path, schema_root)
        _seed_current_pricing(db_path)
        _seed_slot_window(db_path)
        _insert_cases(db_path, "recovery")
        before = _usage_rows(db_path)
        _assert_expected(before)

        # A process may be stopped after the first V2 file has committed.  A
        # later process must continue at the next file, not rebuild facts.
        _upgrade(db_path, schema_root, 0)
        assert _usage_rows(db_path) == before
        _upgrade(db_path, schema_root, 7)
        assert _usage_rows(db_path) == before

        # Re-running the completed upgrade is the restart path used after a
        # health-check timeout or a process replacement.
        _upgrade(db_path, schema_root, 7)
        assert _usage_rows(db_path) == before
        with sqlite_connection(db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 20007
            assert conn.execute(
                "SELECT count(*) FROM schema_migrations WHERE major=2"
            ).fetchone()[0] == 8
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
