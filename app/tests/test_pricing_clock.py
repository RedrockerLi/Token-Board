"""计价时钟：价格表/汇率按事件时间（requested_at）而非写入时刻取用（02-F-06）。

审计发现 ``price_usage_event`` 用 ``date('now')`` / ``strftime('%s','now')`` 取
汇率与时段倍率，历史回填会按“今天的汇率 × 今天的时段”计价，且历史行写入后
不可重算。

用例覆盖三件事：
1. 汇率按 ``requested_at`` 的日期取（今天还有一条更高汇率时不得采用它）；
2. 时段倍率按 ``requested_at`` 的分钟取，而非插入时刻；
3. V2.9 已计价的行在升级到最新 V2 后逐列不变，升级后新插入的行才按新时钟计价；
4. ``requested_at`` 当日及之前没有任何汇率时，非 CNY 规则必须是 ``unrated``，
   不得悄悄用更晚的汇率计价。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.db.migrations import SchemaVersion, apply_sql_migrations
from app.tests.support import PROJECT_ROOT, sqlite_connection


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class PricingClockTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        import shutil
        shutil.copytree(PROJECT_ROOT / "schema", self.root / "schema")
        (self.root / "data").mkdir()
        self.proxy_path = self.root / "data/token-board.db"
        self.dashboard_path = self.root / "data/dashboard.db"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    # -- fixture helpers -------------------------------------------------

    def _latest(self) -> None:
        from app.db.schema_upgrade import ensure_local_databases
        ensure_local_databases(str(self.proxy_path), str(self.dashboard_path),
                               self.root / "schema")

    def _v29_snapshot(self) -> None:
        """Build a V2.9 database: V1 tip first, then the V2 pair target."""
        apply_sql_migrations(str(self.proxy_path), str(self.root / "schema"),
                             "token-board")
        apply_sql_migrations(str(self.proxy_path), str(self.root / "schema"),
                             "token-board", SchemaVersion(2, 9))

    def _upgrade_v2_latest(self) -> None:
        apply_sql_migrations(str(self.proxy_path), str(self.root / "schema"),
                             "token-board")

    def _create_rule(self, pattern: str, *, input_price: float,
                     currency: str = "CNY", slots=()) -> int:
        with sqlite_connection(self.proxy_path) as conn:
            rule_id = conn.execute(
                "INSERT INTO pricing_rules(model_pattern,priority,input_price,"
                "cache_read_price,output_price,currency) VALUES(?,?,?,?,?,?)",
                (pattern, 0, input_price, 0.0, 0.0, currency),
            ).lastrowid
            for start, end, multiplier in slots:
                conn.execute(
                    "INSERT INTO pricing_slots(pricing_rule_id,start_minute,"
                    "end_minute,multiplier) VALUES(?,?,?,?)",
                    (rule_id, start, end, multiplier))
            return rule_id

    def _set_fx(self, base: str, date: str, rate: float) -> None:
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
                "VALUES(?,?,?,?)", (base, "CNY", date, rate))

    def _insert_pending(self, event_id: str, model: str, prompt: int,
                        requested_at: str) -> tuple:
        with sqlite_connection(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,model,prompt_tokens,cache_read_tokens,"
                "completion_tokens,total_tokens,requested_at,pricing_status) "
                "VALUES(?,?,?,0,0,?,?,'pending')",
                (event_id, model, prompt, prompt, requested_at))
            return conn.execute(
                "SELECT pricing_status,equivalent_cost FROM request_log "
                "WHERE event_id=?", (event_id,)).fetchone()

    def _row(self, event_id: str) -> tuple:
        with sqlite_connection(self.proxy_path) as conn:
            return conn.execute(
                "SELECT pricing_status,equivalent_cost,billed_usage_cost "
                "FROM request_log WHERE event_id=?", (event_id,)).fetchone()

    # -- 1) FX rate follows requested_at --------------------------------

    def test_fx_rate_is_taken_at_the_request_date(self) -> None:
        self._latest()
        self._create_rule("clock-usd", input_price=10.0, currency="USD")
        self._set_fx("USD", "2025-06-01", 6.0)
        self._set_fx("USD", _today(), 7.5)

        status, cost = self._insert_pending(
            "clock-fx", "clock-usd", 1_000_000, "2025-06-01T12:00:00Z")
        self.assertEqual(status, "rated")
        # 1,000,000 prompt / 1e6 * 10 USD * 6.0 (2025-06-01 rate) = 60.0
        self.assertAlmostEqual(cost, 60.0)
        # No metered billing contract exists for this import row, so the
        # billable amount stays 0 while the reference cost uses the rate.
        self.assertAlmostEqual(self._row("clock-fx")[2], 0.0)

    # -- 2) slot multiplier follows requested_at minute -----------------

    def test_slot_multiplier_is_taken_at_the_request_minute(self) -> None:
        self._latest()
        self._create_rule("clock-slot", input_price=10.0, slots=(
            (0, 480, 0.5), (480, 1440, 1.0)))

        early = self._insert_pending(
            "clock-slot-early", "clock-slot", 1_000_000,
            "2025-06-01T02:00:00Z")
        late = self._insert_pending(
            "clock-slot-late", "clock-slot", 1_000_000,
            "2025-06-01T12:00:00Z")
        self.assertEqual(early[0], "rated")
        self.assertEqual(late[0], "rated")
        self.assertAlmostEqual(early[1], 5.0)    # ×0.5 window 00:00–08:00
        self.assertAlmostEqual(late[1], 10.0)    # ×1.0 window 08:00–24:00

    # -- 3) V2.9 history stays frozen; new rows use the fixed clock -----

    def test_v29_priced_rows_are_frozen_and_new_rows_use_requested_at(self) -> None:
        self._v29_snapshot()
        self._create_rule("clock-usd", input_price=10.0, currency="USD")
        self._set_fx("USD", "2025-06-01", 6.0)
        self._set_fx("USD", _today(), 7.5)

        # V2.9 prices with the write-time rate: 10 USD × 7.5 = 75.0.
        status, cost = self._insert_pending(
            "clock-frozen", "clock-usd", 1_000_000, "2025-06-01T12:00:00Z")
        self.assertEqual(status, "rated")
        self.assertAlmostEqual(cost, 75.0)
        before = self._row("clock-frozen")

        self._upgrade_v2_latest()
        self.assertEqual(self._row("clock-frozen"), before,
                         "upgrading V2.9 rewrote an already priced row")

        # Rows inserted after the upgrade use the requested_at clock.
        status, cost = self._insert_pending(
            "clock-after-upgrade", "clock-usd", 1_000_000,
            "2025-06-01T12:00:00Z")
        self.assertEqual(status, "rated")
        self.assertAlmostEqual(cost, 60.0)

    # -- 4) no rate at/before requested_at must stay unrated ------------

    def test_missing_rate_at_request_date_is_unrated(self) -> None:
        self._latest()
        self._create_rule("clock-usd-late", input_price=10.0, currency="USD")
        # Only today's rate exists: using it for a 2025 request would be a
        # silent mispricing, so the row must be left unrated instead.
        self._set_fx("USD", _today(), 7.5)

        status, cost = self._insert_pending(
            "clock-no-fx", "clock-usd-late", 1_000_000,
            "2025-06-01T12:00:00Z")
        self.assertEqual(status, "unrated")
        self.assertAlmostEqual(cost, 0.0)


if __name__ == "__main__":
    unittest.main()
