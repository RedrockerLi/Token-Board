#!/usr/bin/env python3
"""Assert the pytest tree stays untouched when the source tree is read-only.

``-p no:cacheprovider`` only stops ``.pytest_cache``; pytest keeps writing
assertion-rewritten bytecode into ``__pycache__``.  That pollution is invisible
to a run on a read-only tree because CPython silently skips unwritable ``.pyc``
files, which is why this gate checks both directions:

1. writable copy: no ``.pytest_cache`` may appear, and ``__pycache__`` must
   appear (otherwise the run would not be exercising the write path at all);
2. read-only copy: the suite must still pass and no file may be written.

Usage: check_readonly_pytest_tree.py <repository-root>
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

COPIED_ENTRIES = ("app", "schema", "pytest.ini")
PROBE_TEST = "app/tests/test_reset_driven_billing.py"
CACHE_NAMES = ("__pycache__", ".pytest_cache")


def copy_repository(source: Path, destination: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    for name in COPIED_ENTRIES:
        origin = source / name
        if not origin.exists():
            raise SystemExit(f"missing repository entry: {origin}")
        if origin.is_dir():
            shutil.copytree(origin, destination / name, ignore=ignore)
        else:
            shutil.copy2(origin, destination / name)


def find_caches(root: Path) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for name in CACHE_NAMES:
        found[name] = sorted(path for path in root.rglob(name) if path.is_dir())
    return found


def remove_caches(root: Path) -> None:
    for path in sorted(find_caches(root)["__pycache__"], reverse=True):
        shutil.rmtree(path, ignore_errors=True)
    for path in root.rglob("*.pyc"):
        path.unlink()


def set_tree_writable(root: Path, *, writable: bool) -> None:
    for directory, subdirs, files in os.walk(root):
        current = Path(directory)
        targets = [current] + [current / name for name in files]
        for target in targets:
            mode = stat.S_IMODE(target.lstat().st_mode)
            if writable:
                mode |= stat.S_IWUSR
                if target.is_dir():
                    mode |= stat.S_IXUSR
            else:
                mode &= ~stat.S_IWUSR
                if target.is_dir():
                    mode |= stat.S_IXUSR
            os.chmod(target, mode)


def run_pytest(tree: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env.pop("PYTHONDONTWRITEBYTECODE", None) is not None:
        print("[TB-READONLY-TREE] note: PYTHONDONTWRITEBYTECODE was set; "
              "unset for the probe so the write path is exercised")
    return subprocess.run(
        [sys.executable, "-m", "pytest", PROBE_TEST, "-q"],
        cwd=tree, env=env, text=True, capture_output=True)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="tb-readonly-tree-"))
    tree = work / "repo"
    tree.mkdir()
    try:
        copy_repository(root, tree)
        before = find_caches(tree)
        polluted = [path for paths in before.values() for path in paths]
        if polluted:
            raise SystemExit(f"the copy already contains caches: {polluted}")

        writable = run_pytest(tree)
        if writable.returncode != 0:
            raise SystemExit("pytest failed in the writable copy:\n" +
                             writable.stdout + writable.stderr)
        after_writable = find_caches(tree)
        if after_writable[".pytest_cache"]:
            raise SystemExit(
                "read-only pytest tree gate failed: -p no:cacheprovider did not "
                f"stop .pytest_cache: {after_writable['.pytest_cache']}")
        if not after_writable["__pycache__"]:
            raise SystemExit(
                "read-only pytest tree gate failed: the writable copy produced no "
                "__pycache__, so the read-only half of the gate would prove nothing")

        remove_caches(tree)
        remaining = find_caches(tree)
        if any(remaining.values()):
            raise SystemExit(f"could not clean the copy: {remaining}")

        set_tree_writable(tree, writable=False)
        denied = False
        try:
            (tree / "should-fail").touch()
        except PermissionError:
            denied = True
        if not denied:
            raise SystemExit("the copy is still writable; the gate cannot run")

        readonly = run_pytest(tree)
        if readonly.returncode != 0:
            failures.append("pytest failed on the read-only tree: " +
                            readonly.stdout + readonly.stderr)
        after_readonly = find_caches(tree)
        written = [path for paths in after_readonly.values() for path in paths]
        if written:
            failures.append(f"pytest wrote into the read-only tree: {written}")

        if failures:
            raise SystemExit("read-only pytest tree gate failed:\n" +
                             "\n".join(failures))
        print("[TB-READONLY-TREE] writable copy: "
              f"{len(after_writable['__pycache__'])} __pycache__ (expected), "
              "0 .pytest_cache; read-only copy: suite passed with no files written")
    finally:
        set_tree_writable(work, writable=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
