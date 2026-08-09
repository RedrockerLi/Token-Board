#!/usr/bin/env python3
"""Pricing-formula refactor equivalence gate (#6 leftover, money-sensitive).

Builds two temp DBs from the SAME schema directory:
  DB-A: migrations 0001..0017  → the live v17 trigger (0014_trigger_fx.sql)
  DB-B: migrations 0001..0018  → the v18 view-based trigger (0018_pricing_view.sql)
seeds identical pricing data, inserts identical `cost_frozen=0` request_log
rows across a matrix of pricing edge cases, and asserts `api_cost` is EXACTLY
equal.  This is the safety net that the view extraction is a byte-equivalent
refactor (no money change) before the column drop (0019) or anything else
lands.  Historical migrations (0002/0004/0006/0007/0012/0014) are frozen and
never rewritten.

Usage:
  python3 pricing_equivalence_test.py <schema_dir> <project_root>
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# (label, model, prompt, cache, completion, requested_at, account_id)
# requested_at is UTC; the trigger derives minute-of-day + date from it.
CASES = [
    ("exact",              "gpt-4o",        1000,     200,      500,      "2026-08-06 10:30:00", None),
    ("wildcard-only",      "gpt-x9",        100,      0,        50,       "2026-08-06 10:30:00", None),
    ("mixed-case",         "GPT-4O",        1000,     200,      500,      "2026-08-06 10:30:00", None),
    ("cache-with-price",   "gpt-x9",        100,      40,       50,       "2026-08-06 10:30:00", None),
    ("slot-mid",           "slot-model",    100,      0,        100,      "2026-08-06 10:30:00", None),
    ("slot-boundary-start","slot-model",    100,      0,        100,      "2026-08-06 10:00:00", None),
    ("slot-boundary-end",  "slot-model",    100,      0,        100,      "2026-08-06 12:00:00", None),
    ("cross-midnight-in",  "slot-model",    100,      0,        100,      "2026-08-06 00:30:00", None),
    ("cross-midnight-out", "slot-model",    100,      0,        100,      "2026-08-06 12:30:00", None),
    ("usd-fx-sameday",     "usd-model",     1000,     0,        0,        "2026-08-06 08:00:00", None),
    ("usd-fx-older",       "usd-model",     1000,     0,        0,        "2026-08-05 08:00:00", None),
    ("usd-no-fx",          "usd-model",     1000,     0,        0,        "2026-08-01 08:00:00", None),
    ("cny-fx-ignored",     "slot-model",    100,      0,        100,      "2026-08-06 12:30:00", None),
    ("zero-tokens",        "gpt-4o",        0,        0,        0,        "2026-08-06 10:30:00", None),
    ("million-scale",      "high-token",    1234567,  987654,   12345678, "2026-08-06 10:30:00", None),
    ("no-match",           "no-such-model", 1000,     0,        500,      "2026-08-06 10:30:00", None),
    ("negative-uncached",  "gpt-4o",        100,      200,      50,       "2026-08-06 10:30:00", None),
    # account_type must not affect write-time pricing (0014+ trigger never
    # joins upstream_accounts; api/plan/agent rows price identically).
    ("acct-api",           "gpt-4o",        1000,     200,      500,      "2026-08-06 10:30:00", 1),
    ("acct-plan",          "gpt-4o",        1000,     200,      500,      "2026-08-06 10:30:00", 2),
    ("acct-agent",         "gpt-4o",        1000,     200,      500,      "2026-08-06 10:30:00", 3),
]

# model_pricing seeds (explicit ids so ORDER BY id LIMIT 1 is deterministic).
PRICING = [
    (1, "gpt-4o",      10,     30,     None,   "CNY"),
    (2, "gpt-*",        1,      2,     0.5,    "CNY"),  # also matches gpt-4o → id 1 wins
    (3, "usd-model",    3,      6,     None,   "USD"),
    (4, "slot-model",   5,      5,     None,   "CNY"),
    (5, "high-token",   0.001,  0.002, None,   "CNY"),
]

SLOTS = [
    (1, 4, 600, 720, 2.0),   # pricing_id 4, minute in [600,720)
    (2, 4, 1430, 90, 3.0),   # cross-midnight: minute >= 1430 OR < 90
]

FX = [
    ("USD", "CNY", "2026-08-05", 7.1),
    ("USD", "CNY", "2026-08-06", 7.2),
]

ACCOUNTS = [
    (1, "api-acct", "api"),
    (2, "plan-acct", "plan"),
    (3, "agent-acct", "agent"),
]


def build_db(schema_dir: Path, max_migration: int) -> str:
    """Migrate a fresh temp DB to the given migration number and seed it."""
    legacy = Path(tempfile.mkdtemp(prefix="pricing-schema-"))
    for mig in schema_dir.glob("*.sql"):
        if int(mig.stem.split("_", 1)[0].split("-", 1)[1]) <= max_migration:
            shutil.copy2(mig, legacy / mig.name)
    db_path = str(Path(tempfile.mkdtemp(prefix="pricing-db-")) / "pricing.db")

    sys.path.insert(0, str(project_root))
    from app.db.migrations import migrate
    migrate(db_path, str(legacy))

    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO model_pricing "
            "(id, model_pattern, input_price, output_price, cache_read_price, "
            " currency) VALUES (?,?,?,?,?,?)",
            PRICING,
        )
        conn.executemany(
            "INSERT INTO pricing_slots "
            "(id, pricing_id, start_minute, end_minute, multiplier) "
            "VALUES (?,?,?,?,?)",
            SLOTS,
        )
        conn.executemany(
            "INSERT INTO fx_rate (base, quote, date, rate) VALUES (?,?,?,?)",
            FX,
        )
        conn.executemany(
            "INSERT INTO upstream_accounts (id, name, account_type) "
            "VALUES (?,?,?)",
            ACCOUNTS,
        )
        # cost_frozen=0 → the live trigger computes api_cost on INSERT.
        rows = [
            (i, model, prompt, cache, comp, prompt + comp, ts, acct)
            for i, (label, model, prompt, cache, comp, ts, acct) in enumerate(CASES, start=1)
        ]
        conn.executemany(
            "INSERT INTO request_log "
            "(id, model, prompt_tokens, cache_read_tokens, completion_tokens, "
            " total_tokens, api_cost, is_streaming, status_code, duration_ms, "
            " attempt_count, requested_at, cost_frozen, account_id) "
            "VALUES (?,?,?,?,?,?,0.0,0,200,0,1,?,0,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def main() -> None:
    global project_root
    ap = argparse.ArgumentParser()
    ap.add_argument("schema_dir", type=str)
    ap.add_argument("project_root", type=str)
    args = ap.parse_args()
    schema_dir = Path(args.schema_dir).resolve()
    project_root = Path(args.project_root).resolve()

    db_a = build_db(schema_dir, 17)  # live v17 trigger (0014)
    db_b = build_db(schema_dir, 18)  # v18 view-based trigger (0018)

    try:
        conn_a = sqlite3.connect(db_a)
        conn_b = sqlite3.connect(db_b)
        try:
            ra = conn_a.execute(
                "SELECT id, api_cost FROM request_log ORDER BY id").fetchall()
            rb = conn_b.execute(
                "SELECT id, api_cost FROM request_log ORDER BY id").fetchall()
        finally:
            conn_a.close()
            conn_b.close()
    finally:
        pass  # temp dirs cleaned by the OS tempfile policy

    assert len(ra) == len(CASES), f"expected {len(CASES)} rows, got {len(ra)}"
    assert ra == rb, (
        "PRICING EQUIVALENCE BROKEN: v17 trigger != v18 view-based trigger\n"
        f"  rows differing: {[(r[0], r[1], b[1]) for r, b in zip(ra, rb) if r != b]}\n"
        "  Do NOT merge a pricing change until this gate passes."
    )
    for i, (row_id, cost) in enumerate(ra, start=1):
        label = CASES[i - 1][0]
        print(f"  {label:<22} api_cost = {cost:.12g}")

    print(f"OK: v17==v18 across {len(CASES)} pricing cases (exact equality)")
    return 0


if __name__ == "__main__":
    main()
