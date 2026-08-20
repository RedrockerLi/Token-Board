"""Static startup guarantees for unattended local upgrades."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StartupContractTest(unittest.TestCase):
    def test_start_script_installs_integrated_dashboard_service(self) -> None:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["bash", "-n", str(root / "start.sh")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        script = (root / "start.sh").read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", script)
        self.assertIn('TB_PROXY_BIN', script)
        self.assertIn('TB_DATA_DIR', script)
        self.assertIn('TB_PYTHON_BIN', script)
        self.assertIn('TB_LEGACY_TIMEZONE', script)
        self.assertIn('TB_DASHBOARD_SERVICE_NAME', script)
        self.assertIn('DASHBOARD_PORT="${TB_DASHBOARD_PORT:-5000}"', script)
        self.assertIn('schema_upgrade.cli', script)
        self.assertNotIn('pgrep -f', script)
        self.assertIn('Description=Token Board Dashboard and Agent Usage Importer',
                      script)
        self.assertIn('systemctl --user enable "$DASHBOARD_SERVICE_NAME"',
                      script)
        self.assertIn('systemctl --user restart "$DASHBOARD_SERVICE_NAME"',
                      script)
        self.assertIn('disable --now "$LEGACY_IMPORT_NAME.timer"', script)
        self.assertNotIn('OnCalendar=', script)
        self.assertNotIn('agent_usage_import.py', script)

        app_js = (root / "static" / "js" / "app.js").read_text(
            encoding="utf-8")
        self.assertIn("fetch('/api/proxy/agent-usage/import'", app_js)

    def test_default_start_generates_boot_service_with_integrated_server(self) -> None:
        root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            for name in ("systemctl",):
                executable = fake_bin / name
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *perf/realtime*) printf '%s\\n' '{\"background_tasks\":{\"codex-importer\":{\"status\":\"ok\"}}}' ;;\n"
                "  *) printf '%s\\n' '{}' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)

            dashboard_service = tmp / "token-dashboard.service"
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "TB_PYTHON_BIN": str(python),
                "TB_DATA_DIR": str(tmp / "data"),
                "TB_DASHBOARD_SERVICE_FILE": str(dashboard_service),
                "TB_IMPORT_SERVICE_FILE": str(tmp / "legacy-import.service"),
                "TB_IMPORT_TIMER_FILE": str(tmp / "legacy-import.timer"),
            })
            result = subprocess.run(
                ["bash", str(root / "start.sh"), "--no-browser"],
                cwd=root, env=env, check=False, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            unit = dashboard_service.read_text(encoding="utf-8")
            self.assertIn("Dashboard and Agent Usage Importer", unit)
            self.assertIn(
                f"WorkingDirectory={root}", unit,
                "systemd treats quotes in WorkingDirectory as part of the path",
            )
            self.assertIn("server.py\" --port 5000", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertIn("WantedBy=default.target", unit)
            verifier = shutil.which("systemd-analyze")
            if verifier:
                verified = subprocess.run(
                    [verifier, "verify", str(dashboard_service)],
                    check=False, capture_output=True, text=True,
                )
                verify_output = verified.stdout + verified.stderr
                if "SO_PASSCRED failed: Operation not permitted" not in verify_output:
                    self.assertEqual(verified.returncode, 0, verify_output)

    def test_failed_dashboard_restart_restores_previous_unit_and_timer(self) -> None:
        root = Path(__file__).resolve().parents[2]
        python = Path(sys.executable)
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = \"--user\" ] && shift\n"
                "if [ \"$1\" = \"restart\" ] && [ \"$2\" = \"token-dashboard\" ]; then exit 1; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            curl = fake_bin / "curl"
            curl.write_text("#!/bin/sh\nprintf '%s\\n' '{}'\n", encoding="utf-8")
            curl.chmod(0o755)

            user_dir = tmp / "config" / "systemd" / "user"
            user_dir.mkdir(parents=True)
            dashboard_service = user_dir / "token-dashboard.service"
            dashboard_service.write_text("old-dashboard\n", encoding="utf-8")
            legacy_service = user_dir / "token-agent-import.service"
            legacy_timer = user_dir / "token-agent-import.timer"
            legacy_service.write_text("old-import-service\n", encoding="utf-8")
            legacy_timer.write_text("old-import-timer\n", encoding="utf-8")

            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "TB_PYTHON_BIN": str(python),
                "TB_DATA_DIR": str(tmp / "data"),
                "XDG_CONFIG_HOME": str(tmp / "config"),
            })
            result = subprocess.run(
                ["bash", str(root / "start.sh"), "--no-browser"],
                cwd=root, env=env, check=False, capture_output=True, text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(dashboard_service.read_text(encoding="utf-8"),
                             "old-dashboard\n")
            self.assertEqual(legacy_service.read_text(encoding="utf-8"),
                             "old-import-service\n")
            self.assertEqual(legacy_timer.read_text(encoding="utf-8"),
                             "old-import-timer\n")
            self.assertFalse((user_dir / "token-dashboard.service.backup").exists())


if __name__ == "__main__":
    unittest.main()
