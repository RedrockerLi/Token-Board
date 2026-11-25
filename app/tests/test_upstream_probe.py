from __future__ import annotations

import unittest

from app.services.upstream_probe import (
    auth_headers, endpoint_url, parse_model_catalog)


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


    def test_google_generate_keeps_the_model_in_the_path(self) -> None:
        versioned = {
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_format": "google"}
        self.assertEqual(
            endpoint_url(versioned, "generate", model="gemini-2.5-pro"),
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/gemini-2.5-pro:generateContent")
        bare = {"base_url": "https://generativelanguage.googleapis.com",
                "api_format": "google"}
        self.assertEqual(
            endpoint_url(bare, "models"),
            "https://generativelanguage.googleapis.com/v1beta/models")
        # A "models/"-prefixed id is normalized, not doubled.
        self.assertEqual(
            endpoint_url(bare, "generate", model="models/gemini-2.5-pro"),
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/gemini-2.5-pro:generateContent")
        # This wire has no body model, so a generate probe without one fails.
        with self.assertRaises(ValueError):
            endpoint_url(bare, "generate")

    def test_google_auth_uses_its_documented_header(self) -> None:
        account = {"base_url": "https://host/v1beta", "api_format": "google",
                   "auth_header": "auto"}
        headers = auth_headers(account, "secret")
        self.assertEqual(headers["x-goog-api-key"], "secret")
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("x-api-key", headers)
        # An explicit bearer still wins (Vertex-style gateways).
        explicit = dict(account, auth_header="bearer")
        self.assertEqual(auth_headers(explicit, "secret")["Authorization"],
                         "Bearer secret")

    def test_google_endpoint_override_names_the_model_abstractly(self) -> None:
        account = {"base_url": "https://host", "api_format": "google",
                   "endpoint_path": "/v1beta/models/{model}:{method}"}
        self.assertEqual(
            endpoint_url(account, "generate", model="gemini-2.5-pro"),
            "https://host/v1beta/models/gemini-2.5-pro:generateContent")

    def test_gemini_model_ids_collapse_to_the_bare_name(self) -> None:
        self.assertEqual(
            endpoint_url({"base_url": "https://host/v1beta",
                          "api_format": "gemini"}, "generate",
                         model="models/gemini-3-pro"),
            "https://host/v1beta/models/gemini-3-pro:generateContent")

    def test_catalog_parsing_covers_both_wire_shapes(self) -> None:
        self.assertEqual(
            parse_model_catalog("google",
                                {"models": [{"name": "models/gemini-2.5-pro"},
                                            {"name": "models/gemini-3-pro"}]}),
            ["gemini-2.5-pro", "gemini-3-pro"])
        self.assertEqual(
            parse_model_catalog("openai",
                                {"data": [{"id": "gpt-5"},
                                          {"id": "gpt-5-mini"}]}),
            ["gpt-5", "gpt-5-mini"])
        # A shape that does not match the wire yields nothing rather than
        # silently reporting an empty-but-valid catalog.
        self.assertEqual(
            parse_model_catalog("google", {"data": [{"id": "x"}]}), [])
        self.assertEqual(parse_model_catalog("openai", None), [])


if __name__ == "__main__":
    unittest.main()
