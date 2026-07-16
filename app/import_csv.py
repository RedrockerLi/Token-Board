"""Import CSV files into the dashboard database.

Usage: python3 -m app.import_csv [--data-dir DATA_DIR] [--db DB_PATH]

Scans data/{platform}/ for CSV files, parses them via registered adapters,
upserts records into the dashboard SQLite DB, then deletes the CSV files.
Day-level granularity: partial months (e.g. a file with only 5 days) are
handled correctly via INSERT OR REPLACE.
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure all adapters are registered
import app.adapters.deepseek  # noqa: F401
import app.adapters.mimo  # noqa: F401

from app.adapters import get_adapter
from app.dashboard_db import DashboardDatabase


def main():
    parser = argparse.ArgumentParser(description="Import CSV files to dashboard DB")
    parser.add_argument(
        "--data-dir", "--data_dir",
        default="data",
        help="Data directory containing platform subdirectories (default: data/)",
    )
    parser.add_argument(
        "--db",
        default="data/dashboard.db",
        help="Dashboard SQLite database path (default: data/dashboard.db)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    db_path = Path(args.db).resolve()

    if not data_dir.exists():
        print(f"[ERROR] Data directory not found: {data_dir}")
        sys.exit(1)

    db = DashboardDatabase(str(db_path))

    total_files = 0
    total_records = 0

    # Scan platform subdirectories
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        platform_name = entry.name

        adapter = get_adapter(platform_name)
        if adapter is None:
            continue

        # Find CSV files
        csv_files = []
        for root, _dirs, files in os.walk(entry):
            for fname in sorted(files):
                if fname.endswith(".csv"):
                    csv_files.append(os.path.join(root, fname))

        if not csv_files:
            continue

        print(f"[{platform_name}] Found {len(csv_files)} CSV file(s)")

        for filepath in csv_files:
            try:
                tus, rus, ces, year, month = adapter.parse(filepath)
                if year == 0 or month == 0:
                    print(f"  SKIP {os.path.basename(filepath)} — could not parse date from content")
                    continue

                # Stamp source metadata
                for rec in tus:
                    rec._year = year
                    rec._month = month
                for rec in rus:
                    rec._year = year
                    rec._month = month
                for rec in ces:
                    rec._year = year
                    rec._month = month

                n = db.upsert_from_ir(tus, rus, ces)
                total_records += n
                total_files += 1

                # Delete CSV after successful import
                os.remove(filepath)
                print(f"  OK  {os.path.basename(filepath)} — {n} records → deleted")

            except Exception as e:
                print(f"  ERR {os.path.basename(filepath)} — {e}")

    # Summary
    counts = db.get_record_count()
    print()
    print(f"Done: {total_files} file(s) imported, {total_records} record(s) upserted")
    print(f"DB now: {counts['token_usage']} token_usage, "
          f"{counts['request_usage']} request_usage, "
          f"{counts['cost_entry']} cost_entry")

    # Sync to cloud — pull latest, add CSV data, push back
    if total_files > 0:
        try:
            from app.sync import sync_dashboard
            proxy_db_path = str(Path(str(db_path)).parent / "proxy.db")
            sync_dashboard(proxy_db_path, str(db_path))
            print("[CSV import] Dashboard synced to cloud")
        except Exception as e:
            print(f"[CSV import] Cloud sync failed: {e}")


if __name__ == "__main__":
    main()
