"""Static startup guarantees for unattended local upgrades."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class StartupContractTest(unittest.TestCase):
    def test_start_script_is_strict_and_uses_project_scoped_overrides(self) -> None:
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
        self.assertIn('TB_LEGACY_TIMEZONE', script)
        self.assertIn('schema_upgrade.cli', script)
        self.assertNotIn('pgrep -f', script)
        self.assertIn('no free dashboard port', script)


if __name__ == "__main__":
    unittest.main()
