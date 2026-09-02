"""Static contracts for quick dashboard starts and explicit upgrades."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class StartupContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.script = (self.root / "start.sh").read_text(encoding="utf-8")

    def test_start_script_is_valid_and_has_two_explicit_modes(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.root / "start.sh")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("START_ALL=false", self.script)
        self.assertIn("if $START_ALL; then", self.script)
        self.assertIn("schema_upgrade.cli", self.script)
        self.assertIn("token-maintenance", self.script)
        self.assertNotIn("TB_DASHBOARD_SERVICE_NAME", self.script)
        self.assertNotIn('systemctl --user enable "$DASHBOARD_SERVICE_NAME"', self.script)
        self.assertNotIn('systemctl --user restart "$DASHBOARD_SERVICE_NAME"', self.script)
        self.assertNotIn("ExecStartPre=", self.script)
        self.assertIn("disable --now", self.script)

    def test_runtime_units_have_distinct_owners(self) -> None:
        self.assertIn("write_proxy_service_unit", self.script)
        self.assertIn("write_maintenance_service_unit", self.script)
        self.assertIn('Description=Token Board Runtime Maintenance', self.script)
        self.assertIn('maintenance.py" --token-board-db', self.script)
        self.assertIn('ExecStart="$PROXY_BIN" --db', self.script)
        self.assertIn('TimeoutStopSec=15', self.script)
        self.assertIn('databases_are_current()', self.script)
        self.assertIn('本地数据库已是最新版，跳过完整升级', self.script)

    def test_fast_mode_does_not_run_upgrade_or_restart_services(self) -> None:
        fast_marker = 'echo "[dash] 快速启动模式：不迁移数据库、不重启后台服务"'
        self.assertIn(fast_marker, self.script)
        self.assertIn('if $START_ALL; then', self.script)
        self.assertIn('-m app.db.schema_upgrade.cli', self.script)


if __name__ == "__main__":
    unittest.main()
