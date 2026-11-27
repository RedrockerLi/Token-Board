#!/usr/bin/env python3
"""Release-time SQLite leak audit for the app test suite.

``pytest.ini`` turns GC-period ResourceWarnings into failures, but that gate
only sees connections whose finalizer runs while pytest owns the unraisable
hook: a connection that is still alive when the interpreter exits is never
reported, and the warning is emitted after the test item that caused it.  This
audit is warning-independent: it instruments ``sqlite3.connect`` for the span
of one pytest session, records an explicit ``close()`` per connection, and
reports

* connections released without ``close()`` (detected in the finalizer), and
* connections still open once the session finished (detected by a GC scan).

Every leak is printed with the source location that created the connection.
The run fails if either count is non-zero.  ``--self-test`` runs the same audit
against a synthetic suite that leaks on purpose, so a passing audit also proves
the detector fires.

Usage: check_sqlite_leaks.py <repository-root> [--tests app/tests]
                             [--min-opened 100] [--no-self-test]
"""

from __future__ import annotations

import gc
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

_AUDIT_FILE = Path(__file__).resolve()
DEFAULT_TESTS = "app/tests"
DEFAULT_MIN_OPENED = 100

SELF_TEST_SUITE = '''\
import gc
import sqlite3

_ALIVE = []


def test_connection_released_without_close():
    conn = sqlite3.connect(":memory:")
    del conn
    gc.collect()


def test_connection_alive_at_session_end():
    _ALIVE.append(sqlite3.connect(":memory:"))
'''

SELF_TEST_INI = "[pytest]\naddopts = -p no:cacheprovider\n"


class ConnectionAudit:
    """Counters shared by every instrumented connection of one session."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0
        self.released_without_close: list[str] = []
        self.alive_unclosed: list[str] = []

    def record_release(self, site: str) -> None:
        self.released_without_close.append(site)

    def scan_alive(self) -> list[str]:
        sites: list[str] = []
        for obj in gc.get_objects():
            if isinstance(obj, _AuditedConnection) and not obj.audit_closed:
                sites.append(obj.audit_site)
        return sites


def _creation_site() -> str:
    for frame in reversed(traceback.extract_stack(limit=8)):
        try:
            filename = Path(frame.filename).resolve()
        except OSError:
            continue
        if filename != _AUDIT_FILE:
            return f"{frame.filename}:{frame.lineno} in {frame.name}"
    return "<unknown>"


class _AuditedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        # Assume the connection never opened until super().__init__ returns, so
        # a constructor failure is not reported as a leak by the finalizer.
        self.audit_closed = True
        self.audit_site = _creation_site()
        self.audit = _ACTIVE_AUDIT
        super().__init__(*args, **kwargs)
        self.audit_closed = False
        audit = self.audit
        if audit is not None:
            audit.opened += 1

    def close(self) -> None:
        if not self.audit_closed:
            self.audit_closed = True
            audit = self.audit
            if audit is not None:
                audit.closed += 1
        super().close()

    def __del__(self) -> None:
        if self.audit_closed:
            return
        audit = getattr(self, "audit", None)
        if audit is not None:
            audit.record_release(getattr(self, "audit_site", "<unknown>"))


_ACTIVE_AUDIT: ConnectionAudit | None = None


def install(audit: ConnectionAudit):
    """Route sqlite3.connect through the audited subclass; return the original."""

    global _ACTIVE_AUDIT
    _ACTIVE_AUDIT = audit
    original = sqlite3.connect

    def audited_connect(*args, **kwargs):
        factory = kwargs.pop("factory", None)
        if factory is not None and factory is not sqlite3.Connection:
            raise RuntimeError(
                "sqlite leak audit cannot instrument a custom connection "
                f"factory: {factory!r}")
        kwargs["factory"] = _AuditedConnection
        return original(*args, **kwargs)

    sqlite3.connect = audited_connect
    sqlite3.dbapi2.connect = audited_connect
    return original


def uninstall(original) -> None:
    global _ACTIVE_AUDIT
    _ACTIVE_AUDIT = None
    sqlite3.connect = original
    sqlite3.dbapi2.connect = original


class LeakAuditPlugin:
    def __init__(self, audit: ConnectionAudit, min_opened: int) -> None:
        self.audit = audit
        self.min_opened = min_opened

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        for _ in range(3):
            gc.collect()
        self.audit.alive_unclosed = self.audit.scan_alive()
        released = self.audit.released_without_close
        alive = self.audit.alive_unclosed
        print(f"[TB-SQLITE-LEAK-AUDIT] opened={self.audit.opened} "
              f"closed={self.audit.closed} "
              f"released_without_close={len(released)} "
              f"alive_unclosed={len(alive)}")
        for site in released:
            print("[TB-SQLITE-LEAK-AUDIT] released without close(): " + site,
                  file=sys.stderr)
        for site in alive:
            print("[TB-SQLITE-LEAK-AUDIT] still open after the session: " + site,
                  file=sys.stderr)
        if released or alive:
            session.exitstatus = 1
        if self.audit.opened < self.min_opened:
            print(f"[TB-SQLITE-LEAK-AUDIT] only {self.audit.opened} connections "
                  f"were instrumented (< {self.min_opened}); the audit is not "
                  "covering the suite", file=sys.stderr)
            session.exitstatus = 1


def _run_pytest(directory: Path, target: str, audit: ConnectionAudit,
                min_opened: int) -> int:
    import pytest

    sys.path.insert(0, str(directory))
    original = install(audit)
    previous = os.getcwd()
    os.chdir(directory)
    try:
        return pytest.main(["-q", target],
                           plugins=[LeakAuditPlugin(audit, min_opened)])
    finally:
        os.chdir(previous)
        uninstall(original)
        sys.path.remove(str(directory))


def write_self_test_case(case_dir: Path) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "pytest.ini").write_text(SELF_TEST_INI, encoding="utf-8")
    (case_dir / "test_leaks.py").write_text(SELF_TEST_SUITE, encoding="utf-8")


def run_self_test_case(case_dir: Path) -> list[str]:
    """Audit a suite that leaks on purpose; the detector must report both leaks."""

    audit = ConnectionAudit()
    code = _run_pytest(case_dir, "test_leaks.py", audit, min_opened=2)
    released = audit.released_without_close
    alive = audit.alive_unclosed
    print(f"[TB-SQLITE-LEAK-SELFTEST] exit={code} "
          f"released_without_close={len(released)} "
          f"alive_unclosed={len(alive)}")
    failures: list[str] = []
    if code == 0:
        failures.append("self-test suite with two leaks was reported as passing")
    if not released:
        failures.append("self-test did not report the released-without-close leak")
    if not alive:
        failures.append("self-test did not report the alive-at-session-end leak")
    return failures


def self_test(work: Path) -> list[str]:
    """Run the negative control in its own process so its leaks cannot leak in."""

    case_dir = work / "self-test"
    write_self_test_case(case_dir)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--self-test-only",
         str(case_dir)],
        text=True, capture_output=True, env=os.environ.copy())
    stream = sys.stdout
    stream.write(completed.stdout)
    stream.flush()
    if completed.stderr:
        sys.stderr.write(completed.stderr)
        sys.stderr.flush()
    if completed.returncode != 0:
        return [f"self-test reported the leaks but exited {completed.returncode}"]
    return []


def audit_suite(root: Path, tests: str, min_opened: int) -> list[str]:
    audit = ConnectionAudit()
    code = _run_pytest(root, tests, audit, min_opened=min_opened)
    failures: list[str] = []
    if code != 0:
        failures.append(f"pytest exited {code}")
    if audit.opened < min_opened:
        failures.append(
            f"only {audit.opened} connections were instrumented (< {min_opened})")
    if audit.released_without_close:
        failures.append(
            f"{len(audit.released_without_close)} connection(s) released without close()")
    if audit.alive_unclosed:
        failures.append(
            f"{len(audit.alive_unclosed)} connection(s) still open after the session")
    return failures


def main() -> None:
    args = list(sys.argv[1:])
    if args[:1] == ["--self-test-only"]:
        case_dir = Path(args[1]).resolve()
        failures = run_self_test_case(case_dir)
        if failures:
            raise SystemExit("sqlite leak self-test failed:\n" + "\n".join(failures))
        return
    root = Path(args.pop(0) if args and not args[0].startswith("-") else ".").resolve()
    tests = DEFAULT_TESTS
    min_opened = DEFAULT_MIN_OPENED
    run_self_test = True
    while args:
        flag = args.pop(0)
        if flag == "--tests":
            tests = args.pop(0)
        elif flag == "--min-opened":
            min_opened = int(args.pop(0))
        elif flag == "--no-self-test":
            run_self_test = False
        else:
            raise SystemExit(f"unknown argument: {flag}")
    work = Path(tempfile.mkdtemp(prefix="tb-sqlite-leak-audit-"))
    failures: list[str] = []
    try:
        if run_self_test:
            failures.extend(self_test(work))
        failures.extend(audit_suite(root, tests, min_opened))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if failures:
        raise SystemExit("sqlite leak audit failed:\n" + "\n".join(failures))
    print("sqlite leak audit passed (no connection left open)")


if __name__ == "__main__":
    main()
