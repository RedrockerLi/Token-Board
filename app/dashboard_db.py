"""Dashboard SQLite database — storage for #/dashboard visualization data.

Stores daily-aggregated usage records with indexes for fast queries.
"""

import sqlite3
from collections import defaultdict

from app.data_loader import safe_float, safe_int
from app.ir import CostEntry, RequestUsage, TokenUsage


# Model display order — lower number = first.  Models not listed default to 99.
MODEL_ORDER = {
    "deepseek-v4-flash": 1,
    "deepseek-v4-pro": 2,
    "mimo-v2.5": 3,
    "Qwen3.5-397B-A17B": 4,
}


class DashboardDatabase:
    """SQLite-backed storage for dashboard visualization data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _create_schema(self):
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key_name TEXT NOT NULL,
                    token_type TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    cost_group_key TEXT DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tu_unique
                    ON token_usage(date, model, api_key_name, token_type, cost_group_key);
                CREATE INDEX IF NOT EXISTS idx_tu_query
                    ON token_usage(api_key_name, date, model);

                CREATE TABLE IF NOT EXISTS request_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key_name TEXT NOT NULL,
                    count INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ru_unique
                    ON request_usage(date, model, api_key_name);
                CREATE INDEX IF NOT EXISTS idx_ru_query
                    ON request_usage(api_key_name, date, model);

                CREATE TABLE IF NOT EXISTS cost_entry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    cost REAL NOT NULL,
                    cost_group_key TEXT DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_unique
                    ON cost_entry(date, model, cost_group_key);
                CREATE INDEX IF NOT EXISTS idx_ce_query
                    ON cost_entry(date, model, cost_group_key);

                CREATE TABLE IF NOT EXISTS model_pricing (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_pattern TEXT NOT NULL UNIQUE,
                    input_price REAL NOT NULL,
                    output_price REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY'
                );

                -- Trigger: recalculate all cost_entry when pricing is inserted
                CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_insert
                AFTER INSERT ON model_pricing
                BEGIN
                    DELETE FROM cost_entry;
                    INSERT INTO cost_entry (date, model, cost, cost_group_key)
                    SELECT
                        tu.date, tu.model,
                        SUM(
                            CASE tu.token_type
                                WHEN 'output' THEN
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.output_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                                ELSE
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.input_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                            END
                        ),
                        tu.cost_group_key
                    FROM token_usage tu
                    GROUP BY tu.date, tu.model, tu.cost_group_key;
                END;

                -- Trigger: recalculate all cost_entry when pricing is updated
                CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_update
                AFTER UPDATE ON model_pricing
                BEGIN
                    DELETE FROM cost_entry;
                    INSERT INTO cost_entry (date, model, cost, cost_group_key)
                    SELECT
                        tu.date, tu.model,
                        SUM(
                            CASE tu.token_type
                                WHEN 'output' THEN
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.output_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                                ELSE
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.input_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                            END
                        ),
                        tu.cost_group_key
                    FROM token_usage tu
                    GROUP BY tu.date, tu.model, tu.cost_group_key;
                END;

                -- Trigger: recalculate all cost_entry when pricing is deleted
                CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_delete
                AFTER DELETE ON model_pricing
                BEGIN
                    DELETE FROM cost_entry;
                    INSERT INTO cost_entry (date, model, cost, cost_group_key)
                    SELECT
                        tu.date, tu.model,
                        SUM(
                            CASE tu.token_type
                                WHEN 'output' THEN
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.output_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                                ELSE
                                    (tu.amount / 1000000.0) * COALESCE(
                                        (SELECT mp.input_price FROM model_pricing mp
                                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                                         ORDER BY mp.id LIMIT 1), 0)
                            END
                        ),
                        tu.cost_group_key
                    FROM token_usage tu
                    GROUP BY tu.date, tu.model, tu.cost_group_key;
                END;
            """)
            conn.commit()
        finally:
            conn.close()

    # ── Upsert from parsed IR records ───────────────────────────────────

    def upsert_from_ir(self, token_usages: list[TokenUsage],
                       request_usages: list[RequestUsage],
                       cost_entries: list[CostEntry]) -> int:
        conn = self._connect()
        total = 0
        try:
            for tu in token_usages:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,?,?,?)""",
                    (tu.date, tu.model, tu.api_key_name,
                     tu.token_type, tu.amount, tu.cost_group_key),
                )
                total += 1

            for ru in request_usages:
                conn.execute(
                    """INSERT OR REPLACE INTO request_usage
                       (date, model, api_key_name, count)
                       VALUES (?,?,?,?)""",
                    (ru.date, ru.model, ru.api_key_name, ru.count),
                )
                total += 1

            for ce in cost_entries:
                conn.execute(
                    """INSERT OR REPLACE INTO cost_entry
                       (date, model, cost, cost_group_key)
                       VALUES (?,?,?,?)""",
                    (ce.date, ce.model, ce.cost, ce.cost_group_key),
                )
                total += 1

            conn.commit()
        finally:
            conn.close()
        return total

    def upsert_proxy_data(self, date: str, model: str,
                          account_name: str, prompt_tokens: int,
                          completion_tokens: int, request_count: int) -> int:
        """Insert proxy usage data. cost_entry is maintained by model_pricing triggers."""
        conn = self._connect()
        total = 0
        try:
            if completion_tokens > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,'output',?,?)""",
                    (date, model, account_name, completion_tokens, account_name),
                )
                total += 1
            if prompt_tokens > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,'input_cache_miss',?,?)""",
                    (date, model, account_name, prompt_tokens, account_name),
                )
                total += 1
            if request_count > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO request_usage
                       (date, model, api_key_name, count)
                       VALUES (?,?,?,?)""",
                    (date, model, account_name, request_count),
                )
                total += 1
            conn.commit()
        finally:
            conn.close()
        return total

    # ── Load to IR lists ──────────────────────────────────────────────

    def load_to_ir(self):
        conn = self._connect()
        try:
            token_usages: list[TokenUsage] = []
            request_usages: list[RequestUsage] = []
            cost_entries: list[CostEntry] = []
            months_set: set[tuple[int, int]] = set()
            api_key_names_set: set[str] = set()
            models_set: set[str] = set()

            for row in conn.execute("SELECT * FROM token_usage"):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                tu = TokenUsage(
                    platform="",
                    date=date,
                    model=row["model"],
                    api_key_name=row["api_key_name"],
                    token_type=row["token_type"],
                    amount=row["amount"],
                    cost_group_key=row["cost_group_key"],
                )
                tu._year = y
                tu._month = m
                token_usages.append(tu)
                months_set.add((y, m))
                api_key_names_set.add(row["api_key_name"])
                models_set.add(row["model"])

            for row in conn.execute("SELECT * FROM request_usage"):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                ru = RequestUsage(
                    platform="",
                    date=date,
                    model=row["model"],
                    api_key_name=row["api_key_name"],
                    count=row["count"],
                )
                ru._year = y
                ru._month = m
                request_usages.append(ru)
                months_set.add((y, m))
                api_key_names_set.add(row["api_key_name"])
                models_set.add(row["model"])

            for row in conn.execute("SELECT * FROM cost_entry"):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                ce = CostEntry(
                    platform="",
                    date=date,
                    model=row["model"],
                    cost=row["cost"],
                    cost_group_key=row["cost_group_key"],
                )
                ce._year = y
                ce._month = m
                cost_entries.append(ce)
                months_set.add((y, m))
                models_set.add(row["model"])

            sorted_months = sorted(months_set, key=lambda x: (x[0], x[1]))
            available_months = [
                {"year": y, "month": m, "label": f"{y}-{m:02d}"}
                for y, m in sorted_months
            ]

            return (
                token_usages,
                request_usages,
                cost_entries,
                available_months,
                sorted(api_key_names_set),
                [],  # platforms — no longer tracked
                _sort_models(models_set),
            )
        finally:
            conn.close()

    def get_record_count(self) -> dict:
        conn = self._connect()
        try:
            return {
                "token_usage": conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0],
                "request_usage": conn.execute("SELECT COUNT(*) FROM request_usage").fetchone()[0],
                "cost_entry": conn.execute("SELECT COUNT(*) FROM cost_entry").fetchone()[0],
            }
        finally:
            conn.close()


def _sort_models(models: set[str]) -> list[str]:
    return sorted(models, key=lambda m: (MODEL_ORDER.get(m, 99), m.lower()))


def _parse_date(date_str: str) -> tuple[int, int]:
    try:
        parts = date_str.split("-")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0, 0
