from __future__ import annotations

from app import create_app
from app.tests.support import AppDatabaseTestCase, sqlite_connection


class AccountFieldValidationTest(AppDatabaseTestCase):
    """The write path rejects wire values the runtime would silently default.

    `parse_api_format` falls back to openai and `resolve_auth_scheme` to bearer,
    so a typo used to look like a working configuration; a negative or
    non-numeric concurrency either tripped the column CHECK (a 500) or was
    stored as text the proxy read back as 0.
    """

    def setUp(self) -> None:
        super().setUp()
        self.app = create_app(str(self.proxy_path), testing=True,
                              start_background_tasks=False)
        self.client = self.app.test_client()

    def _row(self, account_id: int) -> dict:
        with sqlite_connection(self.proxy_path) as conn:
            row = conn.execute(
                "SELECT api_format,auth_scheme,max_concurrency FROM upstreams "
                "WHERE account_id=?", (account_id,)).fetchone()
        return {"api_format": row[0], "auth_scheme": row[1],
                "max_concurrency": row[2]}

    def _create(self, **overrides) -> object:
        payload = {"name": "wire-fields", "account_type": "api",
                   "base_url": "http://upstream.test/v1",
                   "upstream_keys": ["sk-wire-0001"]}
        payload.update(overrides)
        return self.client.post("/api/proxy/accounts", json=payload)

    def _created_id(self, response) -> int:
        self.assertEqual(response.status_code, 201,
                         response.get_data(as_text=True))
        return response.get_json()["id"]

    def test_unknown_wire_format_is_rejected_on_create(self) -> None:
        rejected = self._create(api_format="gpt-4")
        self.assertEqual(rejected.status_code, 400,
                         rejected.get_data(as_text=True))
        self.assertEqual(
            self.client.get("/api/proxy/accounts").get_json(), [])

    def test_gemini_alias_is_stored_as_google(self) -> None:
        account_id = self._created_id(self._create(api_format="GEMINI"))
        self.assertEqual(self._row(account_id)["api_format"], "google")

    def test_unknown_auth_scheme_is_rejected_on_create(self) -> None:
        rejected = self._create(auth_header="basic")
        self.assertEqual(rejected.status_code, 400,
                         rejected.get_data(as_text=True))

    def test_auth_scheme_is_normalized(self) -> None:
        account_id = self._created_id(self._create(auth_header="AUTO"))
        self.assertEqual(self._row(account_id)["auth_scheme"], "auto")

    def test_concurrency_rejects_negative_and_non_numeric(self) -> None:
        for value in (-1, "abc", "3.5", True):
            rejected = self._create(max_concurrency=value)
            self.assertEqual(rejected.status_code, 400,
                             f"{value!r}: {rejected.get_data(as_text=True)}")

    def test_concurrency_accepts_numeric_strings_and_integral_floats(self) -> None:
        account_id = self._created_id(self._create(max_concurrency="3"))
        self.assertEqual(self._row(account_id)["max_concurrency"], 3)
        other_id = self._created_id(
            self._create(name="wire-fields-float", max_concurrency=4.0))
        self.assertEqual(self._row(other_id)["max_concurrency"], 4)

    def test_update_rejects_an_unknown_wire_format_without_writing(self) -> None:
        account_id = self._created_id(self._create(api_format="anthropic"))
        rejected = self.client.put(f"/api/proxy/accounts/{account_id}",
                                   json={"api_format": "gpt-4"})
        self.assertEqual(rejected.status_code, 400,
                         rejected.get_data(as_text=True))
        self.assertEqual(self._row(account_id)["api_format"], "anthropic")

    def test_update_rejects_a_negative_concurrency_without_writing(self) -> None:
        account_id = self._created_id(self._create(max_concurrency=2))
        rejected = self.client.put(f"/api/proxy/accounts/{account_id}",
                                   json={"max_concurrency": -5})
        self.assertEqual(rejected.status_code, 400,
                         rejected.get_data(as_text=True))
        self.assertEqual(self._row(account_id)["max_concurrency"], 2)

    def test_update_normalizes_and_keeps_absent_fields(self) -> None:
        account_id = self._created_id(self._create(max_concurrency=2))
        updated = self.client.put(f"/api/proxy/accounts/{account_id}", json={
            "auth_header": "x-goog-api-key", "max_concurrency": "6"})
        self.assertEqual(updated.status_code, 200,
                         updated.get_data(as_text=True))
        row = self._row(account_id)
        self.assertEqual(row["auth_scheme"], "x-goog-api-key")
        self.assertEqual(row["max_concurrency"], 6)
        # The format was not submitted, so the stored value survives.
        self.assertEqual(row["api_format"], "openai")

    def test_rename_only_update_keeps_wire_fields(self) -> None:
        account_id = self._created_id(
            self._create(api_format="openai_responses", auth_header="bearer",
                         max_concurrency=5))
        renamed = self.client.put(f"/api/proxy/accounts/{account_id}",
                                  json={"name": "wire-fields-renamed"})
        self.assertEqual(renamed.status_code, 200,
                         renamed.get_data(as_text=True))
        self.assertEqual(self._row(account_id),
                         {"api_format": "openai_responses",
                          "auth_scheme": "bearer", "max_concurrency": 5})
