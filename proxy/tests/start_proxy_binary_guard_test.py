#!/usr/bin/env python3
"""The deployment entry point refuses a crash-instrumented binary.

05-TB05-F12: `crash_hook_isolation` proves that the *built* pair is safe
(production binary without the marker, test binary with it), but the deploy
path is what actually runs in production.  A hand-rolled build with
`-DTB_ENABLE_TEST_CRASH`, or a `token_proxy_test` copied over `token_proxy`,
would put `_exit(86)` into a live proxy with nobody watching.  The start
script must therefore refuse such a binary before it builds, verifies the
schema, touches systemd or starts a process.

The check is driven directly (``--verify-binary <path>``) so this test has no
side effects:

  V1  a file that carries the crash marker  -> non-zero exit, stderr names it
  V2  the production binary                 -> exit 0 (the ship shape)
  V3  a plain file without the marker       -> exit 0 (not "reject everything")

The switch is checked for presence first: running the whole start script with
an unknown argument would build and launch a proxy as a side effect, which a
failing test must never do.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "TB_TEST_CRASH_AFTER_SPOOL_SYNC"
VERIFY_FLAG = "--verify-binary"
CHECK_TIMEOUT = 30


def run_start_script(script: Path, project: Path, binary: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), VERIFY_FLAG, str(binary)],
        cwd=str(project), capture_output=True, text=True,
        timeout=CHECK_TIMEOUT,
    )


def output(process: subprocess.CompletedProcess) -> str:
    return (process.stdout or "") + (process.stderr or "")


def main() -> None:
    project = Path(sys.argv[1]).resolve()
    production_binary = Path(sys.argv[2]).resolve()
    script = project / "scripts" / "start-proxy.sh"
    assert script.is_file(), script

    # Presence first: without the flag the script would treat `--verify-binary`
    # as an unknown argument and fall through to a full build/launch.
    source = script.read_text(encoding="utf-8")
    assert VERIFY_FLAG in source, (
        f"{script} does not expose {VERIFY_FLAG}; the deploy path cannot be "
        "checked without rebuilding the world")

    with tempfile.TemporaryDirectory(prefix="token-board-guard-") as raw:
        root = Path(raw)

        marked = root / "token_proxy_with_crash_hook"
        marked.write_text(f"fake binary {MARKER} fake binary\n", encoding="utf-8")
        checked = run_start_script(script, project, marked)
        assert checked.returncode != 0, (checked.returncode, output(checked))
        assert MARKER in output(checked), output(checked)

        clean = root / "token_proxy_without_crash_hook"
        clean.write_text("fake binary without any injection marker\n",
                         encoding="utf-8")
        checked = run_start_script(script, project, clean)
        assert checked.returncode == 0, (checked.returncode, output(checked))

    checked = run_start_script(script, project, production_binary)
    assert checked.returncode == 0, (checked.returncode, output(checked))

    print("start-proxy binary guard passed")


if __name__ == "__main__":
    main()
