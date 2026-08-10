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
