#!/usr/bin/env python3
"""Assert ctest recovers when only ``<build>/Testing/Temporary`` is writable.

CTest refuses to run any test when it cannot create its own log file: on a
read-only build tree it exits 8 with "Cannot create log file: LastTest.log"
after executing zero tests.  ``--output-log``/``-O`` do not help, because they
move the test output, not CTest's own log.  The minimal recovery is to keep
``<build>/Testing/Temporary`` writable, which this gate proves on a throw-away
CMake project:

1. writable build tree -> ctest passes;
2. whole build tree read-only -> ctest exits 8, names the log file and runs
   nothing;
3. only ``Testing/Temporary`` writable -> ctest passes again.

Usage: check_ctest_readonly_recovery.py
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

CMAKE_PROJECT = """\
cmake_minimum_required(VERSION 3.16)
project(tb_ctest_readonly_probe NONE)
enable_testing()
add_test(NAME probe COMMAND "${CMAKE_COMMAND}" -E echo probe-ok)
"""


def run(command: list[str], *, cwd: Path | None = None):
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


def set_read_only(root: Path) -> None:
    for directory, _subdirs, files in os.walk(root):
        current = Path(directory)
        for target in [current] + [current / name for name in files]:
            mode = stat.S_IMODE(target.lstat().st_mode)
            mode &= ~stat.S_IWUSR
            if target.is_dir():
                mode |= stat.S_IXUSR
            os.chmod(target, mode)


def set_writable(root: Path) -> None:
    for directory, _subdirs, files in os.walk(root):
        current = Path(directory)
        for target in [current] + [current / name for name in files]:
            mode = stat.S_IMODE(target.lstat().st_mode)
            mode |= stat.S_IWUSR
            if target.is_dir():
                mode |= stat.S_IXUSR
            os.chmod(target, mode)


def run_ctest(build: Path):
    return run(["ctest", "--test-dir", str(build), "--output-on-failure"])


def main() -> None:
    cmake = shutil.which("cmake")
    ctest = shutil.which("ctest")
    if cmake is None or ctest is None:
        raise SystemExit("cmake/ctest are not on PATH")

    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="tb-ctest-readonly-"))
    source = work / "project"
    build = work / "build"
    source.mkdir()
    (source / "CMakeLists.txt").write_text(CMAKE_PROJECT, encoding="utf-8")
    try:
        configured = run([cmake, "-S", str(source), "-B", str(build)])
        if configured.returncode != 0:
            raise SystemExit("could not configure the probe project:\n" +
                             configured.stdout + configured.stderr)

        baseline = run_ctest(build)
        if baseline.returncode != 0:
            failures.append("ctest failed on the writable build tree: " +
                            baseline.stdout + baseline.stderr)
        temporary = build / "Testing" / "Temporary"
        if not temporary.is_dir():
            raise SystemExit(f"ctest did not create {temporary}")

        set_read_only(build)
        try:
            denied = False
            try:
                (build / "should-fail").touch()
            except PermissionError:
                denied = True
            if not denied:
                raise SystemExit("the build tree is still writable; the gate cannot run")

            read_only = run_ctest(build)
            output = read_only.stdout + read_only.stderr
            if read_only.returncode != 8:
                failures.append(
                    "ctest on a read-only build tree exited "
                    f"{read_only.returncode}, expected 8")
            if "Cannot create log file" not in output:
                failures.append("ctest output does not name the unwritable log file")
            if "probe-ok" in output or "1/1 Test" in output:
                failures.append("ctest executed the test despite the read-only tree")

            # --output-log redirects the *test* output; CTest still needs its
            # own LastTest.log, so the documented workaround must not rescue.
            redirected = run(["ctest", "--test-dir", str(build),
                              "--output-log", str(work / "redirected.log")])
            redirected_output = redirected.stdout + redirected.stderr
            if redirected.returncode != 8:
                failures.append(
                    "ctest --output-log on a read-only build tree exited "
                    f"{redirected.returncode}, expected 8 (the flag only moves "
                    "test output, not CTest's own log)")
            if "Cannot create log file" not in redirected_output:
                failures.append(
                    "ctest --output-log does not report the unwritable log file")

            os.chmod(temporary, stat.S_IMODE(temporary.stat().st_mode) | stat.S_IWUSR)
            recovered = run_ctest(build)
            recovered_output = recovered.stdout + recovered.stderr
            if recovered.returncode != 0:
                failures.append(
                    "ctest did not recover with only Testing/Temporary writable: " +
                    recovered_output)
            if "1/1 Test" not in recovered_output and "100% tests passed" not in recovered_output:
                failures.append("recovered ctest run did not report the probe test")
            if failures:
                raise SystemExit("ctest read-only recovery gate failed:\n" +
                                 "\n".join(failures))
            print("[TB-CTEST-READONLY] writable=0 readonly=8 "
                  "Testing/Temporary-writable=0")
        finally:
            set_writable(build)
    finally:
        set_writable(work)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
