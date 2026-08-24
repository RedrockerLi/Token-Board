"""Command line entry point used by start.sh and maintenance scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .coordinator import ensure_local_databases


def _latest_recovery_paths(data_dir: str | Path) -> tuple[list[str], list[str]]:
    """Locate the most recent incomplete upgrade manifest and backup dir.

    The coordinator writes ``auto-*.manifest.json`` and matching backup
    directories beside the proxy database. On a failed upgrade those are the
    rollback artifacts a user needs to know about, so surface them in the
    error output.
    """
    root = Path(data_dir)
    manifests = sorted(root.glob("auto-*.manifest.json"))
    backups = sorted(root.glob("auto-*.backup"))
    return ([str(p) for p in manifests], [str(p) for p in backups])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ensure Token Board databases are current")
    parser.add_argument("--token-board-db", required=True)
    parser.add_argument("--dashboard-db", required=True)
    parser.add_argument("--schema-dir", required=True)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args(argv)
    try:
        result = ensure_local_databases(
            args.token_board_db, args.dashboard_db, args.schema_dir, args.timezone)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"schema upgrade failed: {exc}", file=sys.stderr)
        manifests, backups = _latest_recovery_paths(Path(args.token_board_db).parent)
        if manifests:
            print("latest upgrade manifest:", file=sys.stderr)
            for path in manifests[-1:]:
                print(f"  {path}", file=sys.stderr)
        if backups:
            print("latest upgrade backup:", file=sys.stderr)
            for path in backups[-1:]:
                print(f"  {path}", file=sys.stderr)
        print("original databases were restored; retry with ./start.sh --all"
              " to resume from the interrupted manifest", file=sys.stderr)
        return 2
    print(json.dumps({name: value.__dict__ for name, value in result.items()},
                     ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
