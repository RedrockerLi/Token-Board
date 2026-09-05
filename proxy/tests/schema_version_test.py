#!/usr/bin/env python3
"""Major-minor migration protocol regression vectors."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import importlib.util
import re
from pathlib import Path


schema_root = Path(sys.argv[1]).resolve()
project_root = Path(sys.argv[2]).resolve()
migrations_path = project_root / "app" / "db" / "migrations.py"
spec = importlib.util.spec_from_file_location("token_board_migrations", migrations_path)
assert spec is not None and spec.loader is not None
migrations = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migrations
spec.loader.exec_module(migrations)
MigrationError = migrations.MigrationError
SchemaVersion = migrations.SchemaVersion
_sql_steps = migrations._sql_steps
migrate = migrations.migrate


def expect_error(fn, text: str) -> None:
    try:
        fn()
    except MigrationError as exc:
        assert text in str(exc), (text, str(exc))
    else:
        raise AssertionError(f"expected MigrationError containing {text!r}")


def main() -> None:
    assert SchemaVersion.from_user_version(19) == SchemaVersion(0, 19)
    assert SchemaVersion.from_user_version(10000) == SchemaVersion(1, 0)
    assert SchemaVersion(1, 10).user_version == 10010

    fresh = Path(tempfile.mkdtemp()) / "token-board.db"
    migrate(str(fresh), str(schema_root), "token-board")
    conn = sqlite3.connect(fresh)
    latest_proxy = max(step.version for step in _sql_steps(schema_root / "token-board" / "v1"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_proxy.user_version
    assert conn.execute(
        "SELECT major,minor,database_name FROM schema_version").fetchone() == (
            latest_proxy.major, latest_proxy.minor, "token-board")
    conn.close()

    legacy = Path(tempfile.mkdtemp()) / "token-board.db"
    migrations.apply_sql_migrations(
        str(legacy), str(schema_root), "token-board",
        target=SchemaVersion(1, 14))
    conn = sqlite3.connect(legacy)
    conn.execute(
        "INSERT INTO accounts(id,uuid,name,valid_from) VALUES(?,?,?,?)",
        (9001, "legacy-api", "legacy-api", "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO accounts(id,uuid,name,valid_from) VALUES(?,?,?,?)",
        (9002, "legacy-plan", "legacy-plan", "2020-01-01"),
    )
    conn.execute(
        "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,"
        "cooldown_policy_json,valid_from) VALUES(?,?,?,?,?,?)",
        ("contract-api", 9001, "metered", "account", '{"kind":"transient"}',
         "2020-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO billing_contracts(uuid,account_id,charge_type,billing_scope,"
        "cooldown_policy_json,valid_from) VALUES(?,?,?,?,?,?)",
        ("contract-plan", 9002, "recurring", "credential",
         '{"kind":"subscription_5h"}', "2020-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    migrations.apply_sql_migrations(str(legacy), str(schema_root), "token-board")
    conn = sqlite3.connect(legacy)
    policies = dict(conn.execute(
        "SELECT account_id,cooldown_policy_json FROM billing_contracts"))
    assert policies[9001] == '{"kind":"none"}', policies
    assert policies[9002] == '{"kind":"subscription_5h"}', policies
    conn.close()

    lifecycle = (project_root / "proxy" / "src" / "store" /
                 "database_lifecycle.cpp").read_text(encoding="utf-8")
    runtime_minor = re.search(
        r"constexpr int kRequiredRuntimeSchemaMinor = (\d+);", lifecycle)
    assert runtime_minor, "C++ runtime schema contract constant is missing"
    assert int(runtime_minor.group(1)) == latest_proxy.minor, (
        "C++ runtime schema contract must match the Token Board V1 tip: "
        f"runtime={runtime_minor.group(1)}, tip={latest_proxy.minor}"
    )

    ordered = Path(tempfile.mkdtemp())
    (ordered / "1-10_ten.sql").write_text("SELECT 10;", encoding="utf-8")
    (ordered / "1-2_two.sql").write_text("SELECT 2;", encoding="utf-8")
    assert [step.version.minor for step in _sql_steps(ordered)] == [2, 10]

    duplicate = Path(tempfile.mkdtemp())
    (duplicate / "1-1_one.sql").write_text("SELECT 1;", encoding="utf-8")
    (duplicate / "1-01_same.sql").write_text("SELECT 1;", encoding="utf-8")
    expect_error(lambda: _sql_steps(duplicate), "duplicate")
    invalid = Path(tempfile.mkdtemp())
    (invalid / "0010_old.sql").write_text("SELECT 1;", encoding="utf-8")
    expect_error(lambda: _sql_steps(invalid), "bad migration filename")

    checksum_dir = Path(tempfile.mkdtemp())
    shutil.copy2(schema_root / "token-board" / "v1" / "1-0_baseline.sql",
                 checksum_dir / "1-0_baseline.sql")
    checksum_db = Path(tempfile.mkdtemp()) / "token-board.db"
    migrate(str(checksum_db), str(checksum_dir), "token-board")
    with (checksum_dir / "1-0_baseline.sql").open("a", encoding="utf-8") as handle:
        handle.write("\n-- forbidden edit\n")
    expect_error(lambda: migrate(str(checksum_db), str(checksum_dir), "token-board"),
                 "checksum mismatch")

    v0 = Path(tempfile.mkdtemp()) / "token-board.db"
    migrate(str(v0), str(schema_root / "token-board" / "v0"), "token-board")
    expect_error(lambda: migrate(str(v0), str(schema_root), "token-board"),
                 "run schema/transitions/0-to-1")

    print("schema version vectors passed")


if __name__ == "__main__":
    main()
