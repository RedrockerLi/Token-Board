#!/usr/bin/env python3
"""One-shot Codex usage importer — run by the systemd user timer
(token-agent-import), decoupled from the dashboard process.

Parses Codex CLI session transcripts (same idempotent logic as the
in-process worker it replaces) and inserts usage rows into the proxy
database.  Runs once and exits; scheduling is owned entirely by the
timer, so the dashboard may be up or down without affecting imports.

Exit codes: 0 = pass completed (0 rows inserted is fine),
            1 = import pass failed,
            2 = local schema setup failed.
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot Codex usage import (timer-driven)")
    parser.add_argument(
        "--proxy-db",
        type=str,
        default=str(SCRIPT_DIR / "data" / "proxy.db"),
        help="Path to proxy SQLite database",
    )
    parser.add_argument(
        "--schema-dir",
        type=str,
        default=str(SCRIPT_DIR / "schema"),
        help="Path to versioned schema directory",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="Asia/Shanghai",
        help="Source timezone for legacy v0→v1 migration",
    )
    args = parser.parse_args(argv)

    # Same unattended local upgrade boundary as create_app / start.sh.
    # ensure_local_databases holds an exclusive flock on the upgrade lock,
    # so racing start.sh's schema upgrade is safe.
    from app.db.schema_upgrade import ensure_local_databases
    try:
        ensure_local_databases(
            args.proxy_db,
            str(Path(args.proxy_db).parent / "dashboard.db"),
            args.schema_dir,
            args.timezone,
        )
    except Exception as exc:
        print(f"schema setup failed: {exc}", file=sys.stderr)
        return 2

    from app.db.proxy_db import ProxyDatabase
    from app.services.codex_import import run_import
    pdb = ProxyDatabase(args.proxy_db)

    # run_import swallows pass exceptions into on_error and still returns 0,
    # so failure must be tracked through the callback, not the return value.
    failed = False

    def on_error(exc: Exception) -> None:
        nonlocal failed
        failed = True
        print(f"codex import failed: {exc}", file=sys.stderr)

    def on_success(inserted: int) -> None:
        if inserted:
            print(f"codex import: inserted {inserted} row(s)")

    total = run_import(pdb, stop_event=None, once=True,
                       on_error=on_error, on_success=on_success)
    print(f"codex import pass done: {total} row(s) total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
