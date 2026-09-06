"""Data loading and the DataStore singleton.

Loads the dashboard archive (dashboard.db) and stores everything as plain
dict rows.
"""

import logging


log = logging.getLogger(__name__)


class DataStore:
    """Holds all parsed rows and derived metadata.

    Usage::

        store = DataStore("/path/to/data")
        store.load()
        len(store.token_usages)
    """

    def __init__(self, data_dir, schema_dir=None):
        self.data_dir = data_dir
        self.schema_dir = schema_dir
        self.token_usages: list[dict] = []
        self.request_usages: list[dict] = []
        self.cost_entries: list[dict] = []
        self.plan_summary: list[dict] = []     # proxy plan monthly economics
        self.available_months: list[dict] = []     # [{"year", "month", "label"}]
        self.api_key_names: list[str] = []
        self.platforms: list[str] = []             # discovered platform names
        self.models: list[str] = []                # unique model names
        # DashboardDatabase accepts only normalized V2 rows.
        self.is_v2 = True

    # ── public API ──────────────────────────────────────────────────────

    def load(self):
        """Rebuild state from the dashboard archive (dashboard.db)."""
        db_path = self.data_dir / "dashboard.db"
        if not db_path.exists():
            log.warning("dashboard archive not found: %s", db_path)
            self._commit([], [], [], [], [], [], [], [])
        else:
            self._load_from_db(str(db_path))

    # ── helpers ─────────────────────────────────────────────────────────

    def _load_from_db(self, db_path: str):
        """Load all records from the dashboard SQLite database."""
        from app.db.dashboard_db import DashboardDatabase

        db = DashboardDatabase(db_path, schema_dir=self.schema_dir)
        conn = db._connect()
        try:
            # Opening the façade already verified the V2 schema.  Do not probe
            # or branch on historical table layouts in the request path.
            self.is_v2 = True
        finally:
            conn.close()
        (
            token_usages,
            request_usages,
            cost_entries,
            available_months,
            api_key_names,
            platforms,
            models,
            plan_summary,
        ) = db.load_rows()

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
