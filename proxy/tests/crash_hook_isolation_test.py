#!/usr/bin/env python3
"""The spool crash-injection hook must exist only in the test binary."""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = b"TB_TEST_CRASH_AFTER_SPOOL_SYNC"


def check(path: Path, *, expected: bool) -> None:
    found = MARKER in path.read_bytes()
    state = "present" if found else "absent"
    want = "present" if expected else "absent"
    assert found == expected, f"{path}: hook marker {state}, expected {want}"


def main() -> None:
    production = Path(sys.argv[1]).resolve()
    test_binary = Path(sys.argv[2]).resolve()
    # The shipped proxy must never carry the environment-triggered _exit path.
    check(production, expected=False)
    # The crash-recovery test only has teeth while its binary does carry it.
    check(test_binary, expected=True)
    print("crash hook isolation passed")


if __name__ == "__main__":
    main()
