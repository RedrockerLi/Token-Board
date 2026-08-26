from __future__ import annotations

import sqlite3
import unittest

from app.services.sync.common import V1_CONFIG_TABLES

from app.tests.support import AppDatabaseTestCase


class PricingLengthTest(AppDatabaseTestCase):
    def _insert_pending(self, event_id: str, model: str, prompt: int,
                        cache: int, completion: int,
                        requested_at: str) -> tuple:
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log"
                "(event_id,model,prompt_tokens,cache_read_tokens,"
                "completion_tokens,total_tokens,requested_at,pricing_status) "
                "VALUES(?,?,?,?,?,?,?,'pending')",
                (event_id, model, prompt, cache, completion,
                 prompt + completion, requested_at),
            )
            return conn.execute(
                "SELECT pricing_status,pricing_rate_id,equivalent_cost "
                "FROM request_log WHERE event_id=?", (event_id,)
            ).fetchone()

    def test_length_tiers_select_highest_threshold_and_inherit_fields(self) -> None:
        db = self.proxy_database()
        db.create_pricing({
            "model_pattern": "tiered-base-*",
            "input_price": 1.0,
            "cache_read_price": 0.5,
            "output_price": 2.0,
            "length_tiers": [
                {"threshold_tokens": 1000, "input_price": 2.0,
                 "cache_read_price": None, "output_price": 4.0},
                {"threshold_tokens": 2000, "input_price": 3.0,
                 "cache_read_price": 0.25, "output_price": None},
            ],
        })

        pricing = db.get_pricing()[0]
        self.assertEqual(
            [(tier["threshold_tokens"], tier["input_price"],
              tier["cache_read_price"], tier["output_price"])
             for tier in pricing["length_tiers"]],
            [(1000, 2.0, None, 4.0), (2000, 3.0, 0.25, None)],
        )

        below = self._insert_pending(
            "tier-below", "tiered-base-model", 999, 0, 1,
            "2999-01-01T00:00:00Z")
        at_first = self._insert_pending(
            "tier-first", "tiered-base-model", 1000, 0, 1,
            "2999-01-01T00:00:00Z")
        at_second = self._insert_pending(
            "tier-second", "tiered-base-model", 2000, 500, 1,
            "2999-01-01T00:00:00Z")

        self.assertEqual(below[0], "rated")
        self.assertAlmostEqual(below[2], (999 + 2) / 1_000_000)
        self.assertAlmostEqual(at_first[2], (1000 * 2 + 4) / 1_000_000)
        self.assertAlmostEqual(
            at_second[2], (1500 * 3 + 500 * 0.25 + 2) / 1_000_000)

        db.create_pricing({
            "model_pattern": "tiered-slot",
            "input_price": 1.0,
            "output_price": 2.0,
            "length_tiers": [{"threshold_tokens": 1000, "input_price": 3.0}],
            "slots": [{"start_minute": 0, "end_minute": 1440,
                       "multiplier": 2.0}],
        })
        tiered_slot = self._insert_pending(
            "tier-slot", "tiered-slot", 1000, 0, 0, "2999-01-01T00:00:00Z")
        self.assertAlmostEqual(tiered_slot[2], 0.006)

    def test_length_tier_updates_are_versioned_and_update_payload_is_compatible(self) -> None:
        db = self.proxy_database()
        pricing_id = db.create_pricing({
            "model_pattern": "history-tiered",
            "input_price": 1.0,
            "output_price": 2.0,
            "length_tiers": [{"threshold_tokens": 1000, "input_price": 3.0}],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            old_valid_from = conn.execute(
                "SELECT valid_from FROM pricing_rates WHERE pricing_rule_id=?",
                (pricing_id,),
            ).fetchone()[0]

        old = self._insert_pending(
            "tier-history-old", "history-tiered", 1000, 0, 0, old_valid_from)
        self.assertAlmostEqual(old[2], 0.003)

        # Omitting length_tiers preserves the current tier configuration.
        self.assertTrue(db.update_pricing(pricing_id, {"input_price": 1.5}))
        self.assertEqual(len(db.get_pricing()[0]["length_tiers"]), 1)

        # Supplying [] explicitly clears tiers and creates another rate.
        self.assertTrue(db.update_pricing(pricing_id, {"length_tiers": []}))
        current = db.get_pricing()[0]
        self.assertEqual(current["length_tiers"], [])
        new = self._insert_pending(
            "tier-history-new", "history-tiered", 1000, 0, 0,
            "2999-01-01T00:00:00Z")
        self.assertAlmostEqual(new[2], 0.0015)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pricing_rates WHERE pricing_rule_id=?",
                (pricing_id,),
            ).fetchone()[0], 3)
            self.assertAlmostEqual(conn.execute(
                "SELECT equivalent_cost FROM request_log WHERE event_id=?",
                ("tier-history-old",),
            ).fetchone()[0], 0.003)

    def test_length_tier_validation_rejects_empty_and_duplicate_rules(self) -> None:
        db = self.proxy_database()
        base = {"model_pattern": "invalid-tier", "input_price": 1,
                "output_price": 2}
        with self.assertRaises(ValueError):
            db.create_pricing({**base, "length_tiers": [
                {"threshold_tokens": 1000},
            ]})
        with self.assertRaises(ValueError):
            db.create_pricing({**base, "length_tiers": [
                {"threshold_tokens": 1000, "input_price": 2},
                {"threshold_tokens": 1000, "output_price": 3},
            ]})
        with self.assertRaises(ValueError):
            db.create_pricing({**base, "length_tiers": [
                {"threshold_tokens": 0, "input_price": 2},
            ]})

    def test_length_tiers_are_part_of_synchronized_configuration(self) -> None:
        self.assertIn("pricing_length_tiers", V1_CONFIG_TABLES)
        with sqlite3.connect(self.proxy_path) as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(pricing_length_tiers)")}
        self.assertEqual(columns, {
            "pricing_rate_id", "threshold_tokens", "input_price",
            "cache_read_price", "output_price",
        })


if __name__ == "__main__":
    unittest.main()
