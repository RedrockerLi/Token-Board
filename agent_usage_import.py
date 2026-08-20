#!/usr/bin/env python3
"""Deprecated manual one-shot wrapper for the server-owned importer.

The production scheduler lives in ``token-dashboard``.  This compatibility
entry point remains useful for maintenance and for an old timer during a
careful upgrade; it deliberately performs only one pass and never starts a
second long-running scheduler.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot Codex usage import")
    parser.add_argument("--proxy-db", default=str(SCRIPT_DIR / "data" / "proxy.db"))
    parser.add_argument("--schema-dir", default=str(SCRIPT_DIR / "schema"))
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args(argv)

    from app.db.schema_upgrade import ensure_local_databases
    dash_db = str(Path(args.proxy_db).resolve().parent / "dashboard.db")
    try:
        ensure_local_databases(
            args.proxy_db, dash_db, args.schema_dir, args.timezone)
        from app.db.proxy_db import ProxyDatabase
        from app.services.codex_import import import_once
        inserted = import_once(
            ProxyDatabase(args.proxy_db, schema_dir=args.schema_dir))
    except Exception as exc:
        print(f"codex import failed: {exc}", file=sys.stderr)
        return 1
    print(f"codex import pass done: {inserted} row(s) inserted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
