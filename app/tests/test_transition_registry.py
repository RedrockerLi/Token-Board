from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db.migrations import SchemaVersion
from app.db.schema_upgrade.compound import select_transitions
from app.db.schema_upgrade.transition_registry import (
    VersionRange,
    discover,
)


class TransitionRegistryTest(unittest.TestCase):
    def test_descriptor_routes_are_version_only_and_ordered(self) -> None:
        root = Path(__file__).resolve().parents[2] / "schema"
        transitions = discover(root)
        self.assertEqual(
            [item.transition_id for item in transitions],
            ["0-to-1", "v1-legacy-agent-billing", "v1-agent-identity"],
        )
        self.assertEqual([item.order for item in transitions], [0, 1, 2])
        for transition in transitions:
            module = transition.load()
            self.assertTrue(callable(getattr(module, "apply", None)))
            self.assertTrue(callable(getattr(module, "verify", None)))
            self.assertFalse(hasattr(module, "needs"))

    def test_current_versions_select_expected_transition(self) -> None:
        root = Path(__file__).resolve().parents[2] / "schema"
        with tempfile.TemporaryDirectory() as directory:
            paths = {"token-board": Path(directory) / "proxy.db",
                     "dashboard": Path(directory) / "dashboard.db"}
            selected = select_transitions(
                root, "local-pair",
                {"token-board": SchemaVersion(0, 19),
                 "dashboard": SchemaVersion(0, 6)}, paths)
            self.assertEqual([item[0].transition_id for item in selected], ["0-to-1"])

            selected = select_transitions(
                root, "local-pair",
                {"token-board": SchemaVersion(1, 6),
                 "dashboard": SchemaVersion(1, 3)}, paths)
            self.assertEqual(
                [item[0].transition_id for item in selected],
                ["v1-legacy-agent-billing", "v1-agent-identity"],
            )

    def test_version_ranges_are_inclusive(self) -> None:
        selector = VersionRange(1, 3, 5)
        self.assertTrue(selector.matches(SchemaVersion(1, 3)))
        self.assertTrue(selector.matches(SchemaVersion(1, 5)))
        self.assertFalse(selector.matches(SchemaVersion(1, 2)))
        self.assertFalse(selector.matches(SchemaVersion(1, 6)))
        self.assertFalse(selector.matches(SchemaVersion(2, 3)))


if __name__ == "__main__":
    unittest.main()
