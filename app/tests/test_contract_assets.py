"""Machine assertions for external/sync contract assets."""

import unittest
from pathlib import Path

from app.routes.contract import ROUTE_CONTRACTS, status_for
from app.services.agent_usage.common import event_id_for
from app.services.agent_usage.importer import _coerce_batch
from app.services.agent_usage.ir import UsageSource
from app.services.agent_usage.registry import ADAPTERS, ADAPTER_SPECS
from app.services.sync.common import (
    CONFIG_TABLE_ALLOWLIST,
    RUNTIME_TABLE_DENYLIST,
    is_config_sync_table,
)


class ContractAssetTest(unittest.TestCase):
    def test_route_status_matrix_is_explicit(self):
        self.assertEqual(status_for("dashboard_delete", {"status": "not_found"}), 404)
        self.assertEqual(status_for("dashboard_upload", {"status": "conflict"}), 409)
        self.assertEqual(status_for("config_upload", {"status": "remote_updated"}), 200)
        self.assertTrue(ROUTE_CONTRACTS["config_test"].force_json)
        self.assertTrue(ROUTE_CONTRACTS["dashboard_delete"].silent_json)
        for contract in ROUTE_CONTRACTS.values():
            self.assertTrue(contract.path.startswith("/api/"))
            self.assertTrue(contract.methods)
            self.assertEqual(set(contract.statuses),
                             set(contract.response_shapes))
            for shape in contract.response_shapes.values():
                self.assertTrue(shape)

    def test_sync_tables_are_allowlist_with_default_deny(self):
        self.assertIn("client_keys", CONFIG_TABLE_ALLOWLIST)
        self.assertIn("request_log", RUNTIME_TABLE_DENYLIST)
        self.assertTrue(is_config_sync_table("client_keys"))
        self.assertFalse(is_config_sync_table("request_log"))
        self.assertFalse(is_config_sync_table("future_machine_local_table"))

    def test_event_identity_and_adapter_manifest_are_explicit(self):
        self.assertEqual(event_id_for("jsonl", "source-a", "line-4"),
                         "jsonl:source-a:line-4")
        self.assertEqual(set(ADAPTERS), set(ADAPTER_SPECS))
        self.assertTrue(all(spec.kind == kind
                            for kind, spec in ADAPTER_SPECS.items()))

    def test_importer_never_recomputes_missing_event_identity(self):
        batch = _coerce_batch(
            (2, [{"model": "m", "event_id": "adapter:stable"},
                 {"model": "m"}]),
            kind="pilot", source_item=UsageSource(Path("source.jsonl")))
        self.assertEqual(batch.record_count, 2)
        self.assertEqual([event.event_id for event in batch.events],
                         ["adapter:stable"])


if __name__ == "__main__":
    unittest.main()
