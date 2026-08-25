"""Machine-checkable contracts for shared runtime primitives."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.core import sqlite_runtime
from app.core.time import (
    EPOCH_MICROSECOND_THRESHOLD,
    EPOCH_MILLISECOND_THRESHOLD,
    format_utc,
    parse_external_timestamp,
    parse_runtime_timestamp,
)
from app.services.fx import FxRateResolver


class RuntimeContractTest(unittest.TestCase):
    def test_time_contract_freezes_runtime_and_external_grammars(self):
        value = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
        self.assertEqual(format_utc(value), "2026-08-25T01:02:03Z")
        self.assertEqual(
            parse_runtime_timestamp("2026-08-25T09:02:03+08:00"), value)
        with self.assertRaises(ValueError):
            parse_runtime_timestamp("2026-08-25 01:02:03")
        self.assertEqual(
            parse_external_timestamp("2026-08-25 01:02:03"), value)
        self.assertEqual(
            parse_external_timestamp(EPOCH_MILLISECOND_THRESHOLD / 1000),
            parse_external_timestamp(EPOCH_MILLISECOND_THRESHOLD))
        self.assertEqual(
            parse_external_timestamp(EPOCH_MICROSECOND_THRESHOLD / 1_000_000),
            parse_external_timestamp(EPOCH_MICROSECOND_THRESHOLD))

    def test_profiles_apply_real_pragmas_and_read_only_is_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime.db"
            conn = sqlite_runtime.connect(path, "proxy_runtime")
            try:
                conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY)")
                self.assertIs(conn.row_factory, sqlite3.Row)
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            finally:
                conn.close()
            readonly = sqlite_runtime.read_only(path)
            try:
                self.assertTrue(sqlite_runtime.PROFILES["agent_external"].read_only)
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute("INSERT INTO item VALUES (1)")
            finally:
                readonly.close()

    def test_transaction_owns_commit_but_not_close_and_rejects_nested_begin(self):
        conn = sqlite_runtime.connect(":memory:", "billing_write")
        with sqlite_runtime.transaction(conn, "immediate"):
            conn.execute("CREATE TABLE item (id INTEGER)")
            with self.assertRaises(RuntimeError):
                with sqlite_runtime.transaction(conn):
                    pass
        conn.execute("INSERT INTO item VALUES (1)")
        conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)
        conn.close()

    def test_fx_resolution_distinguishes_locked_and_provisional(self):
        conn = sqlite_runtime.connect(":memory:", "billing_write")
        try:
            conn.execute("CREATE TABLE fx_rates (base_currency TEXT, quote_currency TEXT, date TEXT, rate REAL)")
            conn.execute("INSERT INTO fx_rates VALUES ('USD','CNY','2026-08-01',7.1)")
            exact = FxRateResolver.resolve(conn, "USD", "CNY", "2026-08-01")
            provisional = FxRateResolver.resolve(conn, "USD", "CNY", "2026-08-02")
            self.assertTrue(exact.exact and exact.locked)
            self.assertFalse(provisional.exact or provisional.locked)
            self.assertEqual(FxRateResolver.finalize(exact), 7.1)
            with self.assertRaises(ValueError):
                FxRateResolver.finalize(provisional)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
