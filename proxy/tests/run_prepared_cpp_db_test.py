#!/usr/bin/env python3
"""Prepare a V2 fixture with Python before running a C++ runtime test."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    schema_root = Path(sys.argv[2]).resolve()
    project_root = Path(sys.argv[3]).resolve()
    sys.path.insert(0, str(project_root))
    from app.db.schema_upgrade import ensure_local_databases

    with tempfile.TemporaryDirectory(prefix="token-board-cpp-test-") as raw:
        database = Path(raw) / "token-board.db"
        dashboard = Path(raw) / "dashboard.db"
        ensure_local_databases(str(database), str(dashboard), schema_root)
        return subprocess.run(
            [str(executable), str(schema_root), str(database)],
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
