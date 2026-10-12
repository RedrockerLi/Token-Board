from __future__ import annotations

import unittest

from app.services.upstream_probe import auth_headers, endpoint_url


class UpstreamProbePolicyTest(unittest.TestCase):
    def test_protocol_default_paths(self) -> None:
        self.assertEqual(
            endpoint_url({"base_url": "https://api.example/v1",
                          "api_format": "openai"}, "chat"),
            "https://api.example/v1/chat/completions")
        self.assertEqual(
            endpoint_url({"base_url": "https://api.example/v1",
                          "api_format": "anthropic"}, "messages"),
            "https://api.example/v1/messages")
        self.assertEqual(
            endpoint_url({"base_url": "https://api.example",
                          "api_format": "openai_responses"}, "responses"),
            "https://api.example/responses")

    def test_opencode_go_catalog_uses_its_declared_v1_path(self) -> None:
        # The opencode-go gateway keeps its catalog under /v1; the subtype
        # carries that fact, not the wire format the user happened to pick.
        account = {"base_url": "https://opencode.ai/zen/go",
                   "api_format": "anthropic", "subtype": "opencode-go"}
        self.assertEqual(endpoint_url(account, "models"),
                         "https://opencode.ai/zen/go/v1/models")
        self.assertEqual(endpoint_url(dict(account, api_format="openai"),
                                      "models"),
                         "https://opencode.ai/zen/go/v1/models")

    def test_models_catalog_is_unchanged_without_a_subtype(self) -> None:
        # Every other upstream keeps the historical "{base}/models" shape.
        self.assertEqual(
            endpoint_url({"base_url": "https://api.example/v1",
                          "api_format": "anthropic"}, "models"),
            "https://api.example/v1/models")
        self.assertEqual(
            endpoint_url({"base_url": "https://api.example",
                          "api_format": "anthropic"}, "models"),
            "https://api.example/models")

    def test_custom_endpoint_and_auth_are_shared(self) -> None:
        account = {"base_url": "https://api.example/v1",
                   "api_format": "anthropic", "endpoint_path": "/probe",
                   "auth_header": "auto"}
        self.assertEqual(endpoint_url(account, "messages"),
                         "https://api.example/probe")
        self.assertEqual(auth_headers(account, "secret")["x-api-key"], "secret")
        self.assertNotIn("Authorization", auth_headers(account, "secret"))

    def test_format_and_auth_defaults_are_case_insensitive(self) -> None:
        account = {"base_url": "https://api.example/v1",
                   "api_format": "AnThRoPiC", "auth_header": "AUTO"}
        self.assertEqual(endpoint_url(account, "messages"),
                         "https://api.example/v1/messages")
        headers = auth_headers(account, "secret")
        self.assertEqual(headers["x-api-key"], "secret")
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
