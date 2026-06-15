"""Data loading and the DataStore singleton.

Scans the data/ directory for platform subdirectories, dispatches CSV
parsing to the appropriate adapter, and stores everything as IR records.
"""

import os
from collections import defaultdict

from app.adapters import get_adapter, list_platforms
from app.ir import CostEntry, RequestUsage, TokenUsage


def _sort_models(models: set) -> list:
    from app.dashboard_db import MODEL_ORDER
    return sorted(models, key=lambda m: (MODEL_ORDER.get(m, 99), m.lower()))


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
    """Holds all parsed IR records and derived metadata.

    Usage::

        store = DataStore("/path/to/data")
        store.load()
        print(len(store.token_usages))
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.token_usages: list[TokenUsage] = []
        self.request_usages: list[RequestUsage] = []
        self.cost_entries: list[CostEntry] = []
        self.available_months: list[dict] = []     # [{"year", "month", "label"}]
        self.api_key_names: list[str] = []
        self.platforms: list[str] = []             # discovered platform names
        self.models: list[str] = []                # unique model names

    # ── public API ──────────────────────────────────────────────────────

    def load(self):
        """Rebuild state from dashboard.db (preferred) or CSV files (fallback)."""
        data_dir = self.data_dir

        # Try dashboard database first
        db_path = data_dir / "dashboard.db"  # data/dashboard.db
        if db_path.exists():
            self._load_from_db(str(db_path))
            return

        # Fallback: scan CSV files (legacy path)
        if not data_dir.exists():
            print(f"[WARN] Data directory not found: {data_dir}")
            self._commit([], [], [], [], [], [], [])
            return

        token_usages: list[TokenUsage] = []
        request_usages: list[RequestUsage] = []
        cost_entries: list[CostEntry] = []
        months_set: set[tuple[int, int]] = set()
        api_key_names_set: set[str] = set()
        models_set: set[str] = set()

        # Each immediate subdirectory of data_dir = one platform
        try:
            for entry in sorted(data_dir.iterdir()):
                if not entry.is_dir():
                    continue
                platform_name = entry.name
                adapter = get_adapter(platform_name)
                if adapter is None:
                    print(f"[WARN] No adapter for platform '{platform_name}', "
                          f"skipping directory '{entry}'")
                    continue

                # Walk the platform directory tree for CSV files
                for root, _dirs, files in os.walk(entry):
                    for fname in sorted(files):
                        if not fname.endswith(".csv"):
                            continue
                        filepath = os.path.join(root, fname)

                        tus, rus, ces, year, month = adapter.parse(filepath)
                        if year == 0 or month == 0:
                            continue  # filename didn't match expected pattern

                        months_set.add((year, month))

                        # Stamp source metadata on every record
                        for rec in tus:
                            rec._year = year
                            rec._month = month
                            api_key_names_set.add(rec.api_key_name)
                            models_set.add(rec.model)
                        for rec in rus:
                            rec._year = year
                            rec._month = month
                            api_key_names_set.add(rec.api_key_name)
                            models_set.add(rec.model)
                        for rec in ces:
                            rec._year = year
                            rec._month = month

                        token_usages.extend(tus)
                        request_usages.extend(rus)
                        cost_entries.extend(ces)

        except OSError as e:
            print(f"[ERROR] Failed to scan data directory: {e}")

        # Sort months
        sorted_months = sorted(months_set, key=lambda x: (x[0], x[1]))
        available_months = [
            {"year": y, "month": m, "label": f"{y}-{m:02d}"}
            for y, m in sorted_months
        ]

        self._commit(
            token_usages,
            request_usages,
            cost_entries,
            available_months,
            sorted(api_key_names_set),
            [],  # platforms no longer tracked
            _sort_models(models_set),
        )

    # ── helpers ─────────────────────────────────────────────────────────

    def _load_from_db(self, db_path: str):
        """Load all records from the dashboard SQLite database."""
        from app.dashboard_db import DashboardDatabase

        db = DashboardDatabase(db_path)
        (
            token_usages,
            request_usages,
            cost_entries,
            available_months,
            api_key_names,
            platforms,
            models,
        ) = db.load_to_ir()

        self._commit(
            token_usages,
            request_usages,
            cost_entries,
            available_months,
            api_key_names,
            platforms,
            models,
        )

    def _commit(self, token_usages, request_usages, cost_entries,
                available_months, api_key_names, platforms, models):
        self.token_usages = token_usages
        self.request_usages = request_usages
        self.cost_entries = cost_entries
        self.available_months = available_months
        self.api_key_names = api_key_names
        self.platforms = platforms
        self.models = models
