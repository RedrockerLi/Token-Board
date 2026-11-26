#!/usr/bin/env python3
"""Production start scripts must configure the proxy without the test suite.

Configuring the test suite validates the Python test interpreter (flask,
requests, jinja2). Production builds started by start.sh and
scripts/start-proxy.sh need no test interpreter, so those scripts configure
with -DBUILD_TESTING=OFF: the tests subdirectory is never added, the interpreter
is never resolved, and a machine whose default python3 lacks the test
dependencies still builds and starts the proxy.

This test pins both halves: the flag in each script, and a real configure with
a fake python3 first on PATH proving no interpreter is resolved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (REPO_ROOT / "start.sh", REPO_ROOT / "scripts" / "start-proxy.sh")
CONFIGURE_FLAGS = ("-DCMAKE_BUILD_TYPE=Release", "-DBUILD_TESTING=OFF")


def configure_commands(script: Path) -> list[str]:
    """Configure command lines of a script, with continuations joined."""
    joined = script.read_text(encoding="utf-8").replace("\\\n", " ")
    commands = []
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped.startswith("cmake") or "--build" in stripped:
            continue
        if " -S " not in stripped and not stripped.startswith("cmake -B "):
            continue
        commands.append(stripped)
    return commands


def check_scripts() -> None:
    for script in SCRIPTS:
        commands = configure_commands(script)
        assert commands, f"{script}: no cmake configure command found"
        relative = script.relative_to(REPO_ROOT)
        for command in commands:
            for flag in CONFIGURE_FLAGS:
                assert flag in command, (
                    f"{relative}: configure must pass {flag}; got: {command}"
                )
        print(f"ok: {relative} configures with {' '.join(CONFIGURE_FLAGS)}")


def write_fake_python(directory: Path) -> Path:
    marker = directory / "fake-python-called.log"
    for name in ("python3", "python3.13"):
        script = directory / name
        script.write_text(
            "#!/bin/sh\n"
            f'echo "{name} $*" >> "{marker}"\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
    return marker


def check_production_configure() -> None:
    cmake = shutil.which("cmake")
    assert cmake, "cmake not found on PATH"
    with tempfile.TemporaryDirectory(prefix="tb-start-scripts-") as temporary:
        temporary_path = Path(temporary)
        fake_bin = temporary_path / "fakebin"
        fake_bin.mkdir()
        marker = write_fake_python(fake_bin)
        build_dir = temporary_path / "build"
        environment = dict(os.environ)
        environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
        command = [
            cmake,
            "-S", str(REPO_ROOT / "proxy"),
            "-B", str(build_dir),
            *CONFIGURE_FLAGS,
        ]
        completed = subprocess.run(
            command, capture_output=True, text=True, env=environment, check=False
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, (
            f"production configure failed with exit {completed.returncode}:\n{output}"
        )
        assert "Python3" not in output, (
            f"test interpreter was resolved by a production configure:\n{output}"
        )
        if marker.exists():
            raise AssertionError(
                "fake python3 was invoked by a production configure:\n"
                + marker.read_text(encoding="utf-8")
            )
        cache = (build_dir / "CMakeCache.txt").read_text(encoding="utf-8")
        assert "BUILD_TESTING:BOOL=OFF" in cache, "BUILD_TESTING was not cached as OFF"
        assert not (build_dir / "tests").exists(), "tests subdirectory was registered"
        print(
            "ok: production configure succeeded with a broken python3 first on "
            "PATH (no interpreter resolved, no tests registered)"
        )


def main() -> int:
    check_scripts()
    check_production_configure()
    print("start scripts configure check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
