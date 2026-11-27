"""TB05-F05：源码门禁（scripts/check_source_size.py）必须保持全绿。

审计时该门禁唯一的失败项是 ``app/db/proxy/accounts_write.py`` 的
``except Exception: pass``（与 03-F-17 同一位置）。门禁是 CTest 用例
``source_size`` 的实现，这里让 pytest 套件也直接守住它，避免“只在 CI 的另一个
入口发现红灯”。
"""

from __future__ import annotations

import subprocess
import sys
import unittest

from app.tests.support import PROJECT_ROOT


class SourceSizeGateTest(unittest.TestCase):
    def test_source_size_gate_is_green(self) -> None:
        script = PROJECT_ROOT / "scripts" / "check_source_size.py"
        result = subprocess.run(
            [sys.executable, str(script), str(PROJECT_ROOT)],
            capture_output=True, text=True)
        self.assertEqual(
            result.returncode, 0,
            "source_size gate failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        self.assertNotIn("silent except/pass", result.stderr)


if __name__ == "__main__":
    unittest.main()
