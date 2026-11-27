#!/usr/bin/env python3
"""Assert the maintenance wake socket rejects over-long paths before bind().

AF_UNIX sun_path holds 108 bytes including the terminating NUL.  A longer path
fails inside ``socket.bind()`` with an ``OSError`` that does not name the limit,
so the operator sees "AF_UNIX path too long" far away from the data directory
that caused it.  This gate drives ``MaintenanceService._prepare_socket`` with
both a short and an over-long path: the short path must bind, the over-long one
must raise a ``RuntimeError`` that reports the byte count, the limit and the
path.

Usage: check_maintenance_socket_path.py <repository-root>
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# The kernel's sun_path is 108 bytes and the last byte is the terminating NUL,
# so 107 usable bytes is an operating-system fact, not a repository setting.
AF_UNIX_PATH_MAX_BYTES = 107
# A socket path under these bases stays far below the limit even when the
# caller's TMPDIR is long.
SHORT_BASES = ("/tmp", "/var/tmp")
PAD_DIR = "d" * 80
PAD_SUBDIR = "e" * 40


def short_base() -> str:
    for candidate in SHORT_BASES:
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return tempfile.gettempdir()


def over_long_socket_path(directory: Path) -> Path:
    # The padding comes from the path itself, so the case stays over the limit
    # even when TMPDIR is short.
    return directory / PAD_DIR / PAD_SUBDIR / "token-maintenance.sock"


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    sys.path.insert(0, str(root))
    from maintenance import MaintenanceService

    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="tb-socket-path-", dir=short_base()))
    socket_dir = Path(tempfile.mkdtemp(prefix="tb-sock-", dir=short_base()))
    try:
        database = work / "data" / "token-board.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        schema_dir = work / "schema"
        schema_dir.mkdir()
        health_path = str(work / "data" / "token-maintenance-health.json")

        long_path = over_long_socket_path(work)
        encoded_length = len(os.fsencode(str(long_path)))
        if encoded_length <= AF_UNIX_PATH_MAX_BYTES:
            raise SystemExit(
                f"gate is broken: constructed path is only {encoded_length} bytes")
        service = MaintenanceService(
            str(database), str(schema_dir), str(long_path), health_path)
        try:
            service._prepare_socket()
        except RuntimeError as error:
            message = str(error)
            for expected in (str(encoded_length), str(AF_UNIX_PATH_MAX_BYTES),
                             str(long_path), "AF_UNIX"):
                if expected not in message:
                    failures.append(
                        f"over-long path error does not mention {expected!r}: {message}")
        except OSError as error:
            failures.append(
                "over-long path still fails with a bare OSError instead of a "
                f"named limit: {error}")
        else:
            failures.append("over-long socket path was accepted")

        socket_path = socket_dir / "token-maintenance.sock"
        short_service = MaintenanceService(
            str(database), str(schema_dir), str(socket_path), health_path)
        short_service._prepare_socket()
        if not socket_path.is_socket():
            failures.append(f"short socket path did not create a socket: {socket_path}")
        else:
            mode = stat.S_IMODE(socket_path.stat().st_mode)
            if mode != 0o600:
                failures.append(f"maintenance socket mode is {oct(mode)}, expected 0o600")
        if short_service.sock is not None:
            short_service.sock.close()
            short_service.sock = None

        if failures:
            raise SystemExit("maintenance socket path gate failed:\n" +
                             "\n".join(failures))
        print(f"maintenance socket path gate passed "
              f"(over-long path rejected at {encoded_length} > "
              f"{AF_UNIX_PATH_MAX_BYTES} bytes)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
