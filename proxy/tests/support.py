"""Shared resource-lifetime helpers for proxy integration tests."""

from __future__ import annotations

import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def sqlite_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Own a test connection and make its transaction outcome explicit."""
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def stop_process(
    process: subprocess.Popen,
    *,
    timeout: float = 3,
    allowed_returncodes: tuple[int, ...] = (0, -15),
    check_returncode: bool = True,
) -> str:
    """Stop a proxy, collect diagnostics, and close its stderr pipe."""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        diagnostics = process.stderr.read() if process.stderr is not None else ""
        if check_returncode and process.returncode not in allowed_returncodes:
            raise AssertionError(diagnostics)
        return diagnostics
    finally:
        if process.stderr is not None:
            process.stderr.close()
