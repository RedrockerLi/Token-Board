"""Upstream usage polling: defensive parsing, failures and the reset chain."""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from app.core.time import format_utc, parse_runtime_timestamp
from app.db.proxy.billing import materialize_period_charges
from app.services.key_usage import poll_usage_once

from app.tests.support import AppDatabaseTestCase, sqlite_connection


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


class _UsageHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server dispatch name
        state = self.server.state
        state["requests"].append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
        })
        status, body = state["response"]
        payload = (body if isinstance(body, (bytes, bytearray))
                   else json.dumps(body).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002 - silence request logs
        return


@contextmanager
def usage_server():
    """A local HTTP fixture that can be re-armed between polls."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UsageHandler)
    server.daemon_threads = True
    server.state = {"response": (200, {"usage": {}}), "requests": []}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class KeyUsagePollerTest(AppDatabaseTestCase):
    def _plan(self, db, base_url: str, *, subtype: str | None = "opencode-go",
              keys=("go-key-1",), valid_from: str | None = None) -> int:
        data = {
            "name": "go-plan", "account_type": "plan", "monthly_price": 10,
            "currency": "CNY", "base_url": base_url,
            "upstream_keys": list(keys),
        }
        if valid_from is not None:
            data["valid_from"] = valid_from
            data["new_valid_froms"] = [valid_from] * len(keys)
        account_id = db.create_account(data)
        if subtype is not None:
            with sqlite_connection(self.proxy_path) as conn:
                conn.execute("UPDATE upstreams SET subtype=? WHERE account_id=?",
                             (subtype, account_id))
                conn.commit()
        return account_id

    def _snapshots(self) -> dict[str, dict]:
        with sqlite_connection(self.proxy_path) as conn:
            return {
                row[0]: json.loads(row[1])
                for row in conn.execute(
                    "SELECT c.uuid,c.usage_json FROM upstream_credentials c "
                    "WHERE c.usage_json IS NOT NULL")
            }

    def test_poll_stores_windows_and_the_first_monthly_period(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            account_id = self._plan(db, f"http://127.0.0.1:{server.server_address[1]}",
                                    keys=("go-key-1", "go-key-2"))
            reset = T0 + timedelta(days=31)
            server.state["response"] = (200, {"usage": {
                # A numeric string is accepted and clamped into 0..100.
                "rolling": {"percent": "142", "resetsAt": format_utc(
                    T0 + timedelta(hours=5))},
                # A zero percentage still reports its window reset; percent
                # only counts usage, it never decides whether a window is live.
                "weekly": {"percent": 0, "resetsAt": format_utc(
                    T0 + timedelta(days=7))},
                "monthly": {"percent": 0, "resetsAt": format_utc(reset)},
            }})

            stats = poll_usage_once(db, now=T0)

            self.assertEqual(stats, {"upstreams": 1, "credentials": 2, "errors": 0})
            self.assertEqual(len(server.state["requests"]), 2)
            for request in server.state["requests"]:
                self.assertEqual(request["path"], "/v1/usage")
                self.assertEqual(request["accept"], "application/json")
                self.assertIn(request["authorization"],
                              {"Bearer go-key-1", "Bearer go-key-2"})
            snapshots = self._snapshots()
            self.assertEqual(len(snapshots), 2)
            for payload in snapshots.values():
                self.assertEqual(payload["checked_at"], format_utc(T0))
                self.assertIsNone(payload["error"])
                self.assertEqual(payload["windows"], {
                    "5h": {"percent": 100,
                           "resets_at": format_utc(T0 + timedelta(hours=5))},
                    "week": {"percent": 0, "resets_at": format_utc(
                        T0 + timedelta(days=7))},
                    "month": {"percent": 0, "resets_at": format_utc(reset)},
                })
                # The raw monthly reset drives the period chain even at 0%.
                self.assertEqual(payload["monthly_period"],
                                 {"start": format_utc(T0), "end": format_utc(reset)})
        with sqlite_connection(self.proxy_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM upstreams WHERE account_id=? AND subtype=?",
                (account_id, "opencode-go")).fetchone()[0], 1)

    def test_poll_skips_malformed_windows_and_unparseable_resets(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            server.state["response"] = (200, {"usage": {
                "rolling": {"percent": None, "resetsAt": format_utc(T0)},
                "weekly": {"percent": 10 ** 400},
                "monthly": {"percent": 7, "resetsAt": "yesterday-ish"},
            }})

            poll_usage_once(db, now=T0)

            payload = next(iter(self._snapshots().values()))
            self.assertEqual(payload["windows"], {
                "month": {"percent": 7, "resets_at": None},
            })
            # An unparseable raw reset establishes no chain at all.
            self.assertIsNone(payload["monthly_period"])

    def test_parse_usage_body_rejects_unusable_windows(self) -> None:
        from app.services.key_usage import parse_usage_body

        windows, resets = parse_usage_body({"usage": {
            "rolling": {"percent": None},
            "weekly": {"percent": "not-a-number"},
            "monthly": {"percent": 10 ** 400,
                        "resetsAt": int(datetime(2027, 1, 1,
                                                 tzinfo=timezone.utc).timestamp())},
        }})

        self.assertEqual(windows, {})
        self.assertEqual(resets, datetime(2027, 1, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            parse_usage_body({"answer": 42})

    def test_poll_ignores_subtype_less_and_unregistered_subtypes(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            self._plan(db, url, subtype=None)
            self._plan(db, url, subtype="not-a-subtype", keys=("go-key-3",))
            # An enabled subtype upstream without a stored plaintext key.
            cloud_only = self._plan(db, url, keys=("go-key-4",))
            with sqlite_connection(self.proxy_path) as conn:
                conn.execute(
                    "DELETE FROM upstream_secrets WHERE credential_uuid IN "
                    "(SELECT uuid FROM upstream_credentials WHERE upstream_id IN "
                    "(SELECT id FROM upstreams WHERE account_id=?))", (cloud_only,))
                conn.commit()

            stats = poll_usage_once(db, now=T0)

            self.assertEqual(stats, {"upstreams": 0, "credentials": 0, "errors": 0})
            self.assertEqual(server.state["requests"], [])
            self.assertEqual(self._snapshots(), {})

    def test_poll_failures_keep_the_previous_snapshot(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            reset = T0 + timedelta(days=31)
            server.state["response"] = (200, {"usage": {
                "rolling": {"percent": 12, "resetsAt": format_utc(T0)},
                "monthly": {"percent": 3, "resetsAt": format_utc(reset)},
            }})
            poll_usage_once(db, now=T0)
            previous = next(iter(self._snapshots().values()))

            for offset, response, message in (
                    (1, (403, {"error": "no subscription"}), "HTTP 403"),
                    (2, (200, b"<html>not json</html>"), "响应不是有效 JSON"),
                    (3, (200, {"nope": 1}), "响应缺少 usage 字段")):
                moment = T0 + timedelta(hours=offset)
                server.state["response"] = response

                stats = poll_usage_once(db, now=moment)

                self.assertEqual(stats["errors"], 1)
                payload = next(iter(self._snapshots().values()))
                self.assertEqual(payload["error"], message)
                self.assertEqual(payload["checked_at"], format_utc(moment))
                self.assertEqual(payload["windows"], previous["windows"])
                self.assertEqual(payload["monthly_period"],
                                 previous["monthly_period"])

    def test_network_failure_records_an_error_without_raising(self) -> None:
        db = self.proxy_database()
        self._plan(db, f"http://127.0.0.1:{_dead_port()}")

        stats = poll_usage_once(db, now=T0)

        self.assertEqual(stats["errors"], 1)
        payload = next(iter(self._snapshots().values()))
        self.assertEqual(payload["error"], "网络错误：ConnectionError")
        self.assertEqual(payload["windows"], {})
        self.assertIsNone(payload["monthly_period"])

    def test_monthly_period_advances_only_through_observed_resets(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            first_end = T0 + timedelta(days=31)
            second_end = first_end + timedelta(days=30)

            def poll(moment: datetime, resets: datetime) -> dict:
                server.state["response"] = (200, {"usage": {
                    "monthly": {"percent": 10, "resetsAt": format_utc(resets)},
                }})
                poll_usage_once(db, now=moment)
                return next(iter(self._snapshots().values()))["monthly_period"]

            self.assertEqual(poll(T0, first_end),
                             {"start": format_utc(T0), "end": format_utc(first_end)})
            # Still inside the observed period: the chain does not advance.
            self.assertEqual(poll(T0 + timedelta(days=10), first_end),
                             {"start": format_utc(T0), "end": format_utc(first_end)})
            # The observation itself reports the roll into a later period.
            self.assertEqual(poll(first_end, second_end),
                             {"start": format_utc(first_end),
                              "end": format_utc(second_end)})
            # A reset earlier than the current start cannot shorten it.
            self.assertEqual(poll(first_end + timedelta(days=1),
                                  first_end - timedelta(days=5)),
                             {"start": format_utc(first_end),
                              "end": format_utc(second_end)})

    def test_poll_without_a_monthly_reset_keeps_the_anchor_period(self) -> None:
        """A poll that reports no monthly boundary must not fabricate a chain."""
        from app.db.proxy.billing import resolve_unit_period
        from app.services.billing_units import BillingUnitResolver

        db = self.proxy_database()
        with usage_server() as server:
            account_id = self._plan(
                db, f"http://127.0.0.1:{server.server_address[1]}",
                valid_from="2026-08-01")
            moment = datetime(2026, 9, 15, tzinfo=timezone.utc)
            for usage in ({"usage": {}},
                          # Same outcome when the reported boundary is unusable.
                          {"usage": {"monthly": {"percent": 5,
                                                 "resetsAt": "tomorrow"}}}):
                with self.subTest(usage=usage):
                    server.state["response"] = (200, usage)

                    poll_usage_once(db, now=moment)

                    self.assertIsNone(
                        next(iter(self._snapshots().values()))["monthly_period"])
                    with sqlite_connection(self.proxy_path) as conn:
                        conn.row_factory = sqlite3.Row
                        unit = next(
                            unit for unit in
                            BillingUnitResolver.proxy_units(conn, at=moment)
                            if unit.account_id == account_id)
                        resolved = resolve_unit_period(conn, unit, moment)
                    # No charge covers this moment: the anchor period, never
                    # the moment the poller happened to run.
                    self.assertEqual(resolved, (
                        datetime(2026, 9, 1, tzinfo=timezone.utc),
                        datetime(2026, 10, 1, tzinfo=timezone.utc)))

    def test_polled_snapshot_drives_the_next_billed_period(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            account_id = self._plan(
                db, f"http://127.0.0.1:{server.server_address[1]}")
            with sqlite_connection(self.proxy_path) as conn:
                conn.row_factory = sqlite3.Row
                first = conn.execute(
                    "SELECT c.period_start,c.period_end FROM billing_period_charges c "
                    "JOIN billing_contracts bc ON bc.id=c.contract_id "
                    "WHERE bc.account_id=?", (account_id,)).fetchone()
            first_end = parse_runtime_timestamp(first["period_end"])
            moment = first_end + timedelta(hours=1)
            observed_end = first_end + timedelta(days=25)
            server.state["response"] = (200, {"usage": {
                "monthly": {"percent": 20, "resetsAt": format_utc(observed_end)},
            }})

            poll_usage_once(db, now=moment)
            materialize_period_charges(self.proxy_path, moment)

            with sqlite_connection(self.proxy_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT c.period_start,c.period_end FROM billing_period_charges c "
                    "JOIN billing_contracts bc ON bc.id=c.contract_id "
                    "WHERE bc.account_id=? ORDER BY c.period_start",
                    (account_id,)).fetchall()
        self.assertEqual([(row["period_start"], row["period_end"]) for row in rows],
                         [(first["period_start"], format_utc(first_end)),
                          (format_utc(first_end), format_utc(observed_end))])

    def test_socket_refresh_wakes_one_poll(self) -> None:
        from maintenance import MaintenanceService

        service = MaintenanceService(
            str(self.proxy_path), str(self.root / "schema"),
            str(self.root / "data/token-maintenance.sock"),
            str(self.root / "data/token-maintenance-health.json"))
        service.proxy_db = self.proxy_database()
        service._prepare_socket()
        called = threading.Event()
        with patch("maintenance.poll_usage_once",
                   side_effect=lambda *args, **kwargs: called.set()):
            loop = threading.Thread(target=service._socket_loop, daemon=True)
            loop.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                    client.sendto(b"USAGE_REFRESH", str(service.socket_path))
                self.assertTrue(called.wait(2.0),
                                "USAGE_REFRESH did not wake a usage poll")
            finally:
                service.stop(join_timeout=2.0)
                loop.join(timeout=2.0)

    def test_shape_deviations_are_logged(self) -> None:
        """A reshaped usage endpoint must be visible at warning level."""
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            server.state["response"] = (200, {"usage": {
                "rolling": {"percent": 37},  # resetsAt became unparseable
                "weekly": {"resetsAt": format_utc(T0)},  # percent unusable
                # "monthly" window disappeared entirely
            }})
            with self.assertLogs("app.services.key_usage",
                                 level="WARNING") as captured:
                poll_usage_once(db, now=T0)

        joined = "\n".join(captured.output)
        self.assertIn("window 'rolling' has an unparseable resetsAt", joined)
        self.assertIn("window 'weekly' has an unusable percent", joined)
        self.assertIn("window 'monthly' missing or not an object", joined)
        self.assertIn("no usable monthly reset", joined)
        # The degraded response still stores a snapshot (defensive parsing
        # is unchanged); the logs are the change-detection surface.
        snapshot = next(iter(self._snapshots().values()))
        self.assertIsNone(snapshot["monthly_period"])
        self.assertIsNone(snapshot["windows"]["5h"]["resets_at"])

    def test_poll_failures_log_the_credential_and_reason(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            server.state["response"] = (500, {"error": "boom"})
            with self.assertLogs("app.services.key_usage",
                                 level="WARNING") as captured:
                poll_usage_once(db, now=T0)

        credential_uuid = next(iter(self._snapshots()))
        joined = "\n".join(captured.output)
        self.assertIn(f"credential {credential_uuid}", joined)
        self.assertIn("HTTP 500", joined)
        self.assertIn("1/1 credentials failed", joined)

    def test_healthy_poll_logs_no_warnings(self) -> None:
        db = self.proxy_database()
        with usage_server() as server:
            self._plan(db, f"http://127.0.0.1:{server.server_address[1]}")
            server.state["response"] = (200, {"usage": {
                "rolling": {"percent": 37, "resetsAt": format_utc(
                    T0 + timedelta(hours=5))},
                "weekly": {"percent": 12, "resetsAt": format_utc(
                    T0 + timedelta(days=7))},
                "monthly": {"percent": 4, "resetsAt": format_utc(
                    T0 + timedelta(days=31))},
            }})
            with self.assertNoLogs("app.services.key_usage", level="WARNING"):
                poll_usage_once(db, now=T0)


if __name__ == "__main__":
    import unittest

    unittest.main()
