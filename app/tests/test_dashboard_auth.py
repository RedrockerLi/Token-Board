from __future__ import annotations

import unittest

from flask import Flask

from app.dashboard_auth import _safe_login_next, install_auth


class DashboardAuthTest(unittest.TestCase):
    def test_login_next_accepts_only_same_origin_paths(self) -> None:
        self.assertEqual(_safe_login_next("/dashboard?tab=usage"),
                         "/dashboard?tab=usage")
        for value in (
                "https://attacker.example/",
                "//attacker.example/",
                "/\\\\attacker.example/",
                "/safe\nLocation: https://attacker.example/"):
            with self.subTest(value=value):
                self.assertEqual(_safe_login_next(value), "/")

    def test_successful_login_uses_validated_next(self) -> None:
        app = Flask(__name__)
        install_auth(app, "test-token")
        client = app.test_client()

        safe = client.post(
            "/login?next=%2Fdashboard", data={"token": "test-token"})
        self.assertEqual(safe.status_code, 302)
        self.assertEqual(safe.headers["Location"], "/dashboard")

        unsafe = client.post(
            "/login?next=https%3A%2F%2Fattacker.example%2F",
            data={"token": "test-token"})
        self.assertEqual(unsafe.status_code, 302)
        self.assertEqual(unsafe.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
