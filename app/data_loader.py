"""Data loading and the DataStore singleton.

Loads the dashboard archive (dashboard.db) and stores everything as IR records.
"""

from collections import defaultdict

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
        self.plan_summary: list[dict] = []     # proxy plan monthly economics
        self.available_months: list[dict] = []     # [{"year", "month", "label"}]
        self.api_key_names: list[str] = []
        self.platforms: list[str] = []             # discovered platform names
        self.models: list[str] = []                # unique model names

    # ── public API ──────────────────────────────────────────────────────

    def load(self):
        """Rebuild state from the dashboard archive (dashboard.db)."""
        db_path = self.data_dir / "dashboard.db"
        if not db_path.exists():
            print(f"[WARN] Dashboard archive not found: {db_path}")
            self._commit([], [], [], [], [], [], [], [])
            return
        self._load_from_db(str(db_path))

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
            plan_summary,
        ) = db.load_to_ir()

        self._commit(
            token_usages,
            request_usages,
            cost_entries,
            available_months,
            api_key_names,
            platforms,
            models,
            plan_summary,
        )

    def _commit(self, token_usages, request_usages, cost_entries,
                available_months, api_key_names, platforms, models,
                plan_summary=None):
        self.token_usages = token_usages
        self.request_usages = request_usages
        self.cost_entries = cost_entries
        self.available_months = available_months
        self.api_key_names = api_key_names
        self.platforms = platforms
        self.models = models
        if plan_summary is not None:
            self.plan_summary = plan_summary


def _track_recency(last_month: dict, month_vol: dict,
                   name: str, y: int, m: int, volume: int) -> None:
    """Track a user's most-recent month and that month's accumulated volume.

    ym = year*100+month (sortable).  A later month restarts the volume; equal
    months accumulate.
    """
    ym = y * 100 + m
    prev = last_month.get(name)
    if prev is None or ym > prev:
        last_month[name] = ym
        month_vol[name] = volume
    elif ym == prev:
        month_vol[name] = month_vol.get(name, 0) + volume
