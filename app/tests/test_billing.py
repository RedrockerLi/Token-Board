from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.db.proxy.billing import materialize_period_charges
from app.services.cost_allocator import (
    compute_proportional_cost,
    compute_proportional_cost_by_model,
    compute_proportional_cost_by_month,
)

from app.tests.support import AppDatabaseTestCase


class BillingTest(AppDatabaseTestCase):
    def test_unbilled_api_account_hard_deletes_live_row_keeps_identity(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "reusable-api", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-reusable"],
        })
        result = db.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone())
            self.assertEqual(conn.execute(
                "SELECT name,account_kind FROM account_identities WHERE id=?",
                (account_id,)).fetchone(), ("reusable-api", "proxy"))
        recreated = db.create_account({
            "name": "reusable-api", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-recreated"],
        })
        self.assertNotEqual(recreated, account_id)

    def test_deleted_plan_exports_frozen_charge_and_usage_via_identity(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "export-after-delete", "account_type": "plan",
            "monthly_price": 12, "currency": "CNY",
            "base_url": "http://example.test", "upstream_keys": ["sk-export"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,total_tokens,status_code,requested_at) "
                "VALUES('export-after-delete','proxy',?,'model',1,1,2,200,"
                "'2026-09-01T00:00:00Z')", (account_id,))
            conn.commit()
        db.delete_account(account_id, mode="immediate")

        result = db.export_to_dashboard(str(self.dashboard_path), 0, db.get_max_log_id())
        self.assertGreaterEqual(result["dashboard_records"], 1)
        with sqlite3.connect(self.dashboard_path) as conn:
            self.assertGreater(conn.execute(
                "SELECT count(*) FROM daily_usage WHERE account_id=?",
                (account_id,)).fetchone()[0], 0)
            self.assertGreater(conn.execute(
                "SELECT count(*) FROM monthly_recurring_costs WHERE account_id=?",
                (account_id,)).fetchone()[0], 0)

    def test_account_delete_consumes_existing_freeze_without_materializing(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "delete-no-materializer", "account_type": "plan",
            "monthly_price": 8, "currency": "CNY",
            "base_url": "http://example.test", "upstream_keys": ["sk-frozen"],
        })
        with patch("app.db.proxy.billing.materialize_all_period_charges",
                   side_effect=AssertionError("delete must not materialize")):
            result = db.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)

    def test_agent_delete_consumes_existing_freeze_without_materializing(self) -> None:
        db = self.proxy_database()
        subscription_id = db.create_agent_subscription({
            "name": "agent-delete-no-materializer", "monthly_price": 5,
            "currency": "CNY",
        })
        with patch("app.db.proxy.billing.materialize_agent_subscription_charges",
                   side_effect=AssertionError("delete must not materialize")):
            result = db.delete_agent_subscription(subscription_id)
        self.assertTrue(result["ok"], result)

    def test_client_key_delete_detaches_history_and_removes_key(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "key-delete-account", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-key"],
        })
        key_value = db.create_key({"account_id": account_id})
        key_id = db.get_keys()[0]["id"]
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "route_set_id,client_key_id,model,status_code,requested_at) "
                "VALUES('key-delete-history','import',?,?,?,?,200,?)",
                (account_id, account_id, key_id, "model", "2026-01-01T00:00:00Z"),
            )
            conn.commit()

        self.assertTrue(db.delete_key(key_id))
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM client_keys WHERE id=?", (key_id,)
            ).fetchone()[0], 0)
            self.assertIsNone(conn.execute(
                "SELECT client_key_id FROM request_log "
                "WHERE event_id='key-delete-history'"
            ).fetchone()[0])

    def test_aggregate_delete_removes_route_and_attached_keys(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "aggregate-target", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-aggregate"],
        })
        aggregate_id = db.create_aggregate({
            "name": "aggregate-delete",
            "entries": [{"pattern": "model", "account_id": account_id,
                         "upstream_model": "model"}],
        })
        key_value = db.create_key({"account_id": aggregate_id})
        key_id = next(k["id"] for k in db.get_keys() if k["key_value"] == key_value)
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,"
                "route_set_id,client_key_id,model,status_code,requested_at) "
                "VALUES('aggregate-delete-history','import',?,?,?,?,200,?)",
                (account_id, aggregate_id, key_id, "model", "2026-01-01T00:00:00Z"),
            )
            conn.commit()

        self.assertTrue(db.delete_aggregate(aggregate_id))
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_sets WHERE id=?", (aggregate_id,)
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_rules WHERE route_set_id=?",
                (aggregate_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM client_keys WHERE id=?", (key_id,)
            ).fetchone()[0], 0)
            row = conn.execute(
                "SELECT route_set_id,client_key_id FROM request_log "
                "WHERE event_id='aggregate-delete-history'"
            ).fetchone()
            self.assertEqual(row, (None, None))

    def test_account_delete_removes_aggregate_rules_before_upstream(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "aggregate-member-delete", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-member-delete"],
        })
        aggregate_id = db.create_aggregate({
            "name": "aggregate-member-delete-target",
            "entries": [{"pattern": "model", "account_id": account_id,
                         "upstream_model": "model"}],
        })

        result = db.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_rules WHERE route_set_id=?",
                (aggregate_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstreams WHERE account_id=?",
                (account_id,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute(
                "PRAGMA foreign_key_check").fetchone())

    def test_finalizer_erases_only_expired_upstream_secrets(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "secret-cleanup", "account_type": "api",
            "base_url": "http://example.test",
            "upstream_keys": ["sk-expired", "sk-live"],
        })
        with sqlite3.connect(self.proxy_path) as conn:
            expired_uuid = conn.execute(
                "SELECT c.uuid FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id "
                "WHERE u.account_id=? "
                "ORDER BY c.runtime_id LIMIT 1", (account_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE upstream_credentials SET deleted_at=? WHERE uuid=?",
                ("2020-01-01T00:00:00Z", expired_uuid),
            )
            conn.commit()

        db.finalize_deferred_deletions()
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_secrets WHERE credential_uuid=?",
                (expired_uuid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstream_secrets s "
                "JOIN upstream_credentials c ON c.uuid=s.credential_uuid "
                "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?",
                (account_id,)).fetchone()[0], 1)

    def test_legacy_cost_helpers_use_v1_row_attribution(self) -> None:
        rows = [
            {"api_key_name": "a", "model": "m", "date": "2026-08-10",
             "_year": 2026, "_month": 8, "cost": 3.0,
             "cost_group_key": "shared"},
            {"api_key_name": "b", "model": "m", "date": "2026-08-10",
             "_year": 2026, "_month": 8, "cost": 7.0,
             "cost_group_key": "shared"},
        ]
        token_rows = [
            {"api_key_name": "a", "amount": 1, "cost_group_key": "shared",
             "date": "2026-08-10", "model": "m"},
            {"api_key_name": "b", "amount": 99, "cost_group_key": "shared",
             "date": "2026-08-10", "model": "m"},
        ]
        self.assertEqual(compute_proportional_cost(token_rows, rows, "a"), 3.0)
        self.assertEqual(compute_proportional_cost_by_model(token_rows, rows, "a"),
                         {"m": 3.0})
        monthly, by_model = compute_proportional_cost_by_month(
            token_rows, rows, "a")
        self.assertEqual(monthly[(2026, 8)], 3.0)
        self.assertEqual(by_model[(2026, 8)]["m"], 3.0)

    def test_database_prices_pending_usage_and_preserves_frozen_history(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "priced", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-priced"],
        })
        db.create_pricing({
            "model_pattern": "model-*", "input_price": 1.0,
            "cache_read_price": 0.5, "output_price": 2.0,
            "currency": "CNY",
        })
        requested_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,cache_read_tokens,total_tokens,"
                "status_code,requested_at,pricing_status) "
                "VALUES('pending','import',?,'model-a',1000000,500000,0,1500000,"
                "200,?,'pending')", (account_id, requested_at))
            pending = conn.execute(
                "SELECT pricing_status,equivalent_cost,billed_usage_cost "
                "FROM request_log WHERE event_id='pending'").fetchone()
            self.assertEqual(pending[0], "rated")
            self.assertAlmostEqual(pending[1], 2.0)
            self.assertAlmostEqual(pending[2], 2.0)

            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,total_tokens,equivalent_cost,"
                "billed_usage_cost,status_code,requested_at,pricing_status) "
                "VALUES('frozen','proxy',?,'model-a',1000000,500000,1500000,0,0,"
                "200,'2025-01-01T00:00:00Z','frozen')", (account_id,))
            frozen = conn.execute(
                "SELECT pricing_status,equivalent_cost FROM request_log "
                "WHERE event_id='frozen'").fetchone()
            self.assertEqual(frozen, ("frozen", 0.0))

    def test_usd_usage_without_historical_fx_is_unrated(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "usd-metered", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-usd"],
        })
        db.create_pricing({
            "model_pattern": "usd-*", "input_price": 1.0,
            "cache_read_price": 0.5, "output_price": 2.0,
            "currency": "USD",
        })
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,total_tokens,status_code,"
                "requested_at,pricing_status) VALUES('usd-no-fx','import',?,"
                "'usd-model',1000000,0,1000000,200,'2026-08-09T00:00:00Z',"
                "'pending')", (account_id,))
            row = conn.execute(
                "SELECT pricing_status,equivalent_cost,"
                "billed_usage_cost FROM request_log WHERE event_id='usd-no-fx'"
            ).fetchone()
            self.assertEqual(row, ("unrated", 0.0, 0.0))

    def test_pricing_priority_and_current_write_time_price(self) -> None:
        db = self.proxy_database()
        account_id = db.create_account({
            "name": "priced-priority", "account_type": "api",
            "base_url": "http://example.test", "upstream_keys": ["sk-pp"],
        })
        # Lower priority number wins, so the specific rule must be created
        # first (priority 0); the generic catch-all follows (priority 1).
        specific = db.create_pricing({"model_pattern": "model-a",
                                      "input_price": 5, "output_price": 10})
        generic = db.create_pricing({"model_pattern": "model-*",
                                     "input_price": 1, "output_price": 2})
        rows = db.get_pricing()
        self.assertEqual([r["model_pattern"] for r in rows],
                         ["model-a", "model-*"])
        requested_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,cache_read_tokens,total_tokens,"
                "status_code,requested_at,pricing_status) "
                "VALUES('prio','import',?,'model-a',1000000,1000000,0,2000000,"
                "200,?,'pending')", (account_id, requested_at))
            row = conn.execute(
                "SELECT equivalent_cost FROM request_log "
                "WHERE event_id='prio'").fetchone()
            # 1M prompt * 5 + 1M completion * 10 = 15: the specific rule wins
            # over the generic model-* rule (which would give 3).
            self.assertAlmostEqual(row[0], 15.0)
        # Reorder moves the specific rule down (generic rule now wins).
        self.assertTrue(db.reorder_pricing_order([generic, specific]))
        self.assertEqual([r["model_pattern"] for r in db.get_pricing()],
                         ["model-*", "model-a"])
        self.assertTrue(db.reorder_pricing_order([specific, generic]))
        # A later update changes the current row in place. Existing request
        # costs remain frozen, while a newly inserted row uses the new price.
        self.assertTrue(db.update_pricing(specific, {"input_price": 6,
                                                     "output_price": 12}))
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO request_log(event_id,source_kind,account_id,model,"
                "prompt_tokens,completion_tokens,total_tokens,status_code,"
                "requested_at,pricing_status) VALUES('prio-new','import',?,'model-a',"
                "1000000,1000000,2000000,200,'2020-01-01T00:00:00Z','pending')",
                (account_id,))
            current = conn.execute(
                "SELECT count(*) FROM pricing_rules WHERE id=?", (specific,)
            ).fetchone()[0]
            self.assertEqual(current, 1)
            self.assertAlmostEqual(conn.execute(
                "SELECT equivalent_cost FROM request_log WHERE event_id='prio'"
            ).fetchone()[0], 15.0)
            self.assertAlmostEqual(conn.execute(
                "SELECT equivalent_cost FROM request_log WHERE event_id='prio-new'"
            ).fetchone()[0], 18.0)

    def test_pricing_delete_is_physical_and_cascades_children(self) -> None:
        db = self.proxy_database()
        pricing_id = db.create_pricing({
            "model_pattern": "delete-me", "input_price": 1,
            "output_price": 2,
            "slots": [{"start_minute": 0, "end_minute": 1440,
                       "multiplier": 1.5}],
            "length_tiers": [{"threshold_tokens": 1000,
                              "input_price": 2}],
        })
        self.assertTrue(db.delete_pricing(pricing_id))
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pricing_rules WHERE id=?", (pricing_id,)
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pricing_slots WHERE pricing_rule_id=?",
                (pricing_id,)
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM pricing_length_tiers WHERE pricing_rule_id=?",
                (pricing_id,)
            ).fetchone()[0], 0)

    def test_recurring_cross_month_anchor_and_cancellation(self) -> None:
        db = self.proxy_database()
        now = datetime.now(timezone.utc)
        valid_from = now.replace(day=1).strftime("%Y-%m-%d")
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) "
                "VALUES('billing.cancellation_mode','immediate')")
            conn.commit()
        account_id = db.create_account({
            "name": "plan-cross", "account_type": "plan", "currency": "CNY",
            "monthly_price": 20, "valid_from": valid_from,
            "base_url": "http://example.test", "upstream_keys": ["sk-cross"],
        })
        # Anchor is the current day → the first period is a partial month that
        # runs into next month; materializing twice stays idempotent.
        materialize_period_charges(str(self.proxy_path), now)
        materialize_period_charges(str(self.proxy_path), now)
        with sqlite3.connect(self.proxy_path) as conn:
            rows = conn.execute(
                "SELECT recurring_charge,base_currency "
                "FROM billing_period_charges bc JOIN billing_contracts c "
                "ON c.id=bc.contract_id WHERE c.account_id=?", (account_id,)
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0][0], 20.0)
            self.assertEqual(rows[0][1], "CNY")
        # Immediate cancellation stops routing and removes the account.
        result = db.delete_account(account_id, mode="immediate")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["deferred"], result)
        self.assertNotIn(account_id, [a["id"] for a in db.get_accounts()])
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM route_sets WHERE id=? AND enabled=1",
                (account_id,)).fetchone()[0], 0)

    def test_account_delete_uses_current_global_mode(self) -> None:
        db = self.proxy_database()
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", "immediate"),
            )
            conn.commit()
        account_id = db.create_account({
            "name": "global-mode-plan", "account_type": "plan",
            "monthly_price": 20, "base_url": "http://example.test",
            "upstream_keys": ["sk-stale-policy"],
        })

        # The deletion mode is read from the global setting at deletion time.
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "UPDATE sync_settings SET value='period_end' "
                "WHERE key='billing.cancellation_mode'"
            )
            conn.commit()

        self.assertEqual(
            db.get_plan_billing_config()["cancellation_mode"], "end_of_period"
        )
        result = db.delete_account(account_id)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["deferred"], result)
        self.assertEqual(result["cancellation_mode"], "end_of_period")
        self.assertIn(account_id, [a["id"] for a in db.get_accounts()])
        with sqlite3.connect(self.proxy_path) as conn:
            account = conn.execute(
                "SELECT lifecycle_state,deleted_at FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            self.assertEqual(account[0], "active")
            self.assertIsNotNone(account[1])
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(billing_contracts)"
            )}
            self.assertNotIn("cancellation_policy", columns)
            self.assertEqual(conn.execute(
                "SELECT enabled FROM upstreams WHERE account_id=?", (account_id,)
            ).fetchone()[0], 1)

    def test_account_delete_uses_latest_key_expiry(self) -> None:
        db = self.proxy_database()
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", "end_of_period"),
            )
            conn.commit()
        account_id = db.create_account({
            "name": "latest-key-expiry", "account_type": "plan",
            "monthly_price": 20, "base_url": "http://example.test",
            "upstream_keys": ["sk-early"],
        })
        fixed_now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        with sqlite3.connect(self.proxy_path) as conn:
            upstream_id = conn.execute(
                "SELECT id FROM upstreams WHERE account_id=?", (account_id,)
            ).fetchone()[0]
            first = conn.execute(
                "SELECT uuid FROM upstream_credentials WHERE upstream_id=?",
                (upstream_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE upstream_credentials SET valid_from=? WHERE uuid=?",
                ("2026-08-01", first),
            )
            second = "credential-latest-expiry"
            conn.execute(
                "INSERT INTO upstream_credentials"
                "(uuid,runtime_id,upstream_id,position,key_masked,valid_from,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (second, 990001, upstream_id, 1, "sk-late", "2026-08-15",
                 "2026-08-15T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO upstream_secrets(credential_uuid,secret_value) VALUES(?,?)",
                (second, "sk-late"),
            )
            conn.commit()

        # Billing is frozen at the cycle boundary; deletion only consumes the
        # already materialized facts and must not create them.
        materialize_period_charges(str(self.proxy_path), fixed_now)

        with patch("app.db.proxy.lifecycle.utc_now", return_value=fixed_now):
            result = db.delete_account(account_id)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["deferred"], result)
        self.assertEqual(result["effective_deleted_at"], "2026-09-14T23:59:59Z")
        with sqlite3.connect(self.proxy_path) as conn:
            account_deleted_at = conn.execute(
                "SELECT deleted_at FROM accounts WHERE id=?", (account_id,)
            ).fetchone()[0]
            self.assertEqual(account_deleted_at, "2026-09-14T23:59:59Z")
            expiries = dict(conn.execute(
                "SELECT uuid,deleted_at FROM upstream_credentials WHERE upstream_id=?",
                (upstream_id,),
            ).fetchall())
            self.assertEqual(expiries[first], "2026-08-31T23:59:59Z")
            self.assertEqual(expiries[second], "2026-09-14T23:59:59Z")

    def test_pending_account_deletion_can_be_cancelled(self) -> None:
        db = self.proxy_database()
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", "end_of_period"),
            )
            conn.commit()
        account_id = db.create_account({
            "name": "restorable-plan", "account_type": "plan",
            "valid_from": "2026-08-01", "monthly_price": 20,
            "base_url": "http://example.test", "upstream_keys": ["sk-restore"],
            "new_valid_froms": ["2026-08-01"],
        })
        fixed_now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        materialize_period_charges(str(self.proxy_path), fixed_now)
        with patch("app.db.proxy.lifecycle.utc_now", return_value=fixed_now):
            deleted = db.delete_account(account_id)
            self.assertTrue(deleted["deferred"], deleted)
            self.assertEqual(deleted["effective_deleted_at"],
                             "2026-08-31T23:59:59Z")

            cancelled = db.cancel_account_deletion(account_id)

        self.assertTrue(cancelled["ok"], cancelled)
        self.assertEqual(cancelled["restored_credentials"], 1)
        with sqlite3.connect(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT lifecycle_state,deleted_at FROM accounts WHERE id=?",
                (account_id,)).fetchone(), ("active", None))
            self.assertIsNone(conn.execute(
                "SELECT c.deleted_at FROM upstream_credentials c "
                "JOIN upstreams u ON u.id=c.upstream_id WHERE u.account_id=?",
                (account_id,)).fetchone()[0])
            self.assertEqual(conn.execute(
                "SELECT enabled FROM upstreams WHERE account_id=?",
                (account_id,)).fetchone()[0], 1)
        self.assertIn(account_id, [row["id"] for row in db.get_accounts()])

    def test_expired_account_deletion_cannot_be_cancelled(self) -> None:
        db = self.proxy_database()
        with sqlite3.connect(self.proxy_path) as conn:
            conn.execute(
                "INSERT INTO sync_settings(key,value) VALUES(?,?)",
                ("billing.cancellation_mode", "end_of_period"),
            )
            conn.commit()
        account_id = db.create_account({
            "name": "expired-plan", "account_type": "plan",
            "valid_from": "2026-08-01", "monthly_price": 20,
            "base_url": "http://example.test", "upstream_keys": ["sk-expired"],
            # Pin the credential's billing anchor too. Without this field the
            # fixture falls back to the machine's creation date, making the
            # effective deletion boundary depend on today's day-of-month.
            "new_valid_froms": ["2026-08-01"],
        })
        scheduled_at = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
        expired_at = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        materialize_period_charges(str(self.proxy_path), scheduled_at)
        with patch("app.db.proxy.lifecycle.utc_now", return_value=scheduled_at):
            self.assertTrue(db.delete_account(account_id)["deferred"])
        with patch("app.db.proxy.lifecycle.utc_now", return_value=expired_at):
            cancelled = db.cancel_account_deletion(account_id)
            self.assertFalse(cancelled["ok"], cancelled)
            self.assertEqual(db.finalize_deferred_deletions(), 1)

        with sqlite3.connect(self.proxy_path) as conn:
            self.assertIsNone(conn.execute(
                "SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone())
            self.assertIsNotNone(conn.execute(
                "SELECT id FROM account_identities WHERE id=?",
                (account_id,)).fetchone())


if __name__ == "__main__":
    import unittest
    unittest.main()
