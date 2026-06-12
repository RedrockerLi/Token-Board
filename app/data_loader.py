"""CSV data loading and the DataStore singleton.

Reads cost-*.csv and amount-*.csv files from the data/ directory recursively,
extracting year/month from filenames and injecting source metadata into each row.
"""

import csv
import os
import re
from collections import defaultdict


def safe_int(val, default=0):
    """Convert to int, treating empty string as default."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val, default=0.0):
    """Convert to float, treating empty string as default."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class DataStore:
    """Holds all parsed CSV records and derived metadata.

    Usage::

        store = DataStore("/path/to/data")
        store.load()
        print(len(store.cost_records))
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.cost_records: list[dict] = []
        self.amount_records: list[dict] = []
        self.available_months: list[dict] = []   # [{"year", "month", "label"}]
        self.api_key_names: list[str] = []

    # ── public API ──────────────────────────────────────────────────────────

    def load(self):
        """Scan data_dir recursively, parse all CSVs, rebuild internal state."""
        cost_records = []
        amount_records = []
        months_set = set()

        data_dir = self.data_dir
        if not data_dir.exists():
            print(f"[WARN] Data directory not found: {data_dir}")
            self.cost_records = []
            self.amount_records = []
            self.available_months = []
            self.api_key_names = []
            return

        for root, _dirs, files in os.walk(data_dir):
            for fname in files:
                if not fname.endswith(".csv"):
                    continue
                parsed = self._parse_filename(fname)
                if parsed is None:
                    continue
                csv_type, year, month = parsed
                filepath = os.path.join(root, fname)
                months_set.add((year, month))

                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            cleaned = {k.strip(): v.strip() for k, v in row.items()}
                            cleaned["_source_year"] = year
                            cleaned["_source_month"] = month
                            cleaned["_source_file"] = fname
                            if csv_type == "cost":
                                cost_records.append(cleaned)
                            else:
                                amount_records.append(cleaned)
                except Exception as e:
                    print(f"[ERROR] Failed to read {filepath}: {e}")

        # Collect unique api_key_names from amount records
        api_key_names_set = set()
        for r in amount_records:
            name = r.get("api_key_name", "").strip()
            if name:
                api_key_names_set.add(name)

        # Sort months
        sorted_months = sorted(months_set, key=lambda x: (x[0], x[1]))
        available_months = [
            {"year": y, "month": m, "label": f"{y}-{m:02d}"}
            for y, m in sorted_months
        ]

        self.cost_records = cost_records
        self.amount_records = amount_records
        self.available_months = available_months
        self.api_key_names = sorted(api_key_names_set)

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_filename(filename: str) -> tuple[str, int, int] | None:
        """Extract (type, year, month) from 'cost-2026-5.csv'."""
        m = re.match(r"(cost|amount)-(\d{4})-(\d{1,2})\.csv$", filename)
        if m:
            return m.group(1), int(m.group(2)), int(m.group(3))
        return None
