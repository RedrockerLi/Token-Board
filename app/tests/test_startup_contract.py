"""Static contracts for quick dashboard starts and explicit upgrades."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class StartupContractTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        self.script = (self.root / "start.sh").read_text(encoding="utf-8")
        self.proxy_script = (self.root / "scripts/start-proxy.sh").read_text(
            encoding="utf-8")

    def test_start_script_is_valid_and_has_two_explicit_modes(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.root / "start.sh")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("START_ALL=false", self.script)
        self.assertIn("DEBUG_MODE=false", self.script)
        self.assertIn('exec bash "$SCRIPT_DIR/scripts/start-proxy.sh" --debug', self.script)
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
        self.assertIn('StandardOutput=null', self.script)
        self.assertIn('StandardError=journal', self.script)
        self.assertIn('databases_are_current()', self.script)
        self.assertIn('本地数据库已是最新版，跳过完整升级', self.script)

    def test_proxy_launch_commands_match_cpp_runtime_cli(self) -> None:
        for name, content in (
                ("start.sh", self.script),
                ("scripts/start-proxy.sh", self.proxy_script)):
            commands = [
                line.strip() for line in content.splitlines()
                if "$PROXY_BIN" in line
            ]
            self.assertTrue(commands, name)
            self.assertFalse(
                any("--schema-dir" in line for line in commands),
                f"{name} passes Python-only --schema-dir to token_proxy:\n"
                + "\n".join(commands),
            )

    def test_debug_mode_is_foreground_and_restores_active_service(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.root / "scripts/start-proxy.sh")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--debug", self.proxy_script)
        self.assertIn("--log-level debug", self.proxy_script)
        self.assertIn("systemctl --user stop", self.proxy_script)
        self.assertIn("systemctl --user start", self.proxy_script)
        self.assertIn("StandardOutput=null", self.proxy_script)
        self.assertIn("StandardError=journal", self.proxy_script)

    def test_fast_mode_does_not_run_upgrade_or_restart_services(self) -> None:
        fast_marker = 'echo "[dash] 快速启动模式：不迁移数据库、不重启后台服务"'
        self.assertIn(fast_marker, self.script)
        self.assertIn('if $START_ALL; then', self.script)
        self.assertIn('-m app.db.schema_upgrade.cli', self.script)


if __name__ == "__main__":
    unittest.main()
