"""Unified logging configuration for the dashboard and maintenance processes.

The proxy writes structured records to stderr/journald.  The Python side has
two long-lived processes (the dashboard and ``token-maintenance``); both need
a durable sink instead of relying on an unconfigured root logger.  This module
installs one rotating file handler plus a stream handler so failures and
background-task tracebacks survive the launcher redirecting stderr.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

_LOG_MARKER = "_token_board_logging"
_CONFIGURED = False
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5
_FILENAME = "dashboard.log"


class _SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that creates files with mode 0600.

    Logs may contain local paths and upstream error text, so the file must
    stay private even when the process umask is permissive.
    """

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_APPEND if "a" in self.mode else os.O_TRUNC
        fd = os.open(self.baseFilename, flags, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def _formatter() -> logging.Formatter:
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    return formatter


def configure_logging(data_dir, *, file_level=logging.INFO,
                      stream_level=logging.WARNING,
                      filename: str = _FILENAME) -> None:
    """Install the process-wide logging handlers exactly once.

    File creation failures must not stop the application: the stream handler
    is still installed, and the caller keeps running (the launcher may have
    redirected stderr, but that is preferable to a startup failure).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    formatter = _formatter()
    handlers: list[logging.Handler] = []

    try:
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / filename
        file_handler = _SecureRotatingFileHandler(
            log_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _LOG_MARKER, True)
        handlers.append(file_handler)
    except OSError as exc:
        # Fall through to the stream handler; a read-only data directory
        # should not prevent the UI from starting.
        print(f"token-board: cannot open {Path(data_dir) / filename}: {exc}",
              file=sys.stderr)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(stream_level)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _LOG_MARKER, True)
    handlers.append(stream_handler)

    root.setLevel(min(file_level, stream_level))
    for handler in handlers:
        root.addHandler(handler)

    # Access logs would dominate the file; application warnings/errors and
    # background-task tracebacks are the useful signal here.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    _CONFIGURED = True


def reset_logging() -> None:
    """Remove handlers installed by :func:`configure_logging` (tests only)."""
    global _CONFIGURED
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _LOG_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    _CONFIGURED = False
