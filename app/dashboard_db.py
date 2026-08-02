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


# ── Cost recalculation SQL ──────────────────────────────────────────────────
# Shared SELECT that recomputes proxy cost_entry from token_usage with
# cache-aware pricing: output → output_price; input_cache_hit →
# cache_read_price (falls back to input_price); the rest → input_price.
# Used both inside the model_pricing triggers and for the one-off recompute
# when upgrading legacy databases.  Accounts registered in account_types as
# 'plan' get cost 0 (subscription covers usage — see proxy_plan_summary for
# the real subscription cost and the api-billed virtual cost).
#
# CSV-imported groups are EXCLUDED: those rows already carry the price from
# the CSV file (cost_entry.source='csv'), so recomputing them from
# model_pricing would double-count (imported price + model price).
_MP_COST_SELECT = """
    SELECT
        tu.date, tu.model,
        SUM(
            CASE WHEN COALESCE(
                (SELECT at.account_type FROM account_types at
                 WHERE at.account_name = tu.cost_group_key), 'api') = 'plan'
            THEN 0
            ELSE
            CASE tu.token_type
                WHEN 'output' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.output_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                WHEN 'input_cache_hit' THEN
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT COALESCE(mp.cache_read_price, mp.input_price) FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
                ELSE
                    (tu.amount / 1000000.0) * COALESCE(
                        (SELECT mp.input_price FROM model_pricing mp
                         WHERE LOWER(tu.model) GLOB LOWER(mp.model_pattern)
                         ORDER BY mp.id LIMIT 1), 0)
            END
            END
        ),
        tu.cost_group_key,
        'proxy'
    FROM token_usage tu
    WHERE NOT EXISTS (
        SELECT 1 FROM cost_entry ce
        WHERE ce.source = 'csv'
          AND ce.date = tu.date
          AND ce.model = tu.model
          AND ce.cost_group_key = tu.cost_group_key
    )
    GROUP BY tu.date, tu.model, tu.cost_group_key
"""

_TR_MP_INSERT = (
    "CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_insert\n"
    "AFTER INSERT ON model_pricing\n"
    "BEGIN\n"
    "    DELETE FROM cost_entry WHERE source = 'proxy';\n"
    "    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)\n"
    + _MP_COST_SELECT + ";\n"
    "END;\n"
)

_TR_MP_UPDATE = (
    "CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_update\n"
    "AFTER UPDATE ON model_pricing\n"
    "BEGIN\n"
    "    DELETE FROM cost_entry WHERE source = 'proxy';\n"
    "    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)\n"
    + _MP_COST_SELECT + ";\n"
    "END;\n"
)

_TR_MP_DELETE = (
    "CREATE TRIGGER IF NOT EXISTS tr_mp_refresh_delete\n"
    "AFTER DELETE ON model_pricing\n"
    "BEGIN\n"
    "    DELETE FROM cost_entry WHERE source = 'proxy';\n"
    "    INSERT INTO cost_entry (date, model, cost, cost_group_key, source)\n"
    + _MP_COST_SELECT + ";\n"
    "END;\n"
)


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
                    cost_group_key TEXT DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'proxy'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_unique
                    ON cost_entry(date, model, cost_group_key, source);
                CREATE INDEX IF NOT EXISTS idx_ce_query
                    ON cost_entry(date, model, cost_group_key);

                CREATE TABLE IF NOT EXISTS model_pricing (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_pattern TEXT NOT NULL UNIQUE,
                    input_price REAL NOT NULL,
                    output_price REAL NOT NULL,
                    cache_read_price REAL,
                    currency TEXT NOT NULL DEFAULT 'CNY'
                );

                -- Account type registry (mirrors upstream_accounts.account_type
                -- for proxy-exported data; plan accounts get cost 0 in
                -- cost_entry, their subscription + virtual costs live in
                -- proxy_plan_summary).
                CREATE TABLE IF NOT EXISTS account_types (
                    account_name TEXT PRIMARY KEY,
                    account_type TEXT NOT NULL DEFAULT 'api'
                );

                -- Per-month plan economics, written on export:
                -- subscription_cost = monthly price (only for months with usage)
                -- virtual_cost = api-billed amount of all plan usage that month
                CREATE TABLE IF NOT EXISTS proxy_plan_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    month TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    subscription_cost REAL NOT NULL DEFAULT 0,
                    virtual_cost REAL NOT NULL DEFAULT 0,
                    UNIQUE(month, account_name)
                );

                """
                + _TR_MP_INSERT + _TR_MP_UPDATE + _TR_MP_DELETE
            )
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
                       (date, model, cost, cost_group_key, source)
                       VALUES (?,?,?,?,'csv')""",
                    (ce.date, ce.model, ce.cost, ce.cost_group_key),
                )
                total += 1

            conn.commit()
        finally:
            conn.close()
        return total

    def upsert_proxy_data(self, date: str, model: str,
                          account_name: str, prompt_tokens: int,
                          completion_tokens: int, cache_read_tokens: int,
                          request_count: int,
                          account_type: str = "api") -> int:
        """Insert proxy usage data. cost_entry is maintained by model_pricing triggers.

        prompt_tokens is the total input (including cache hits), so the miss
        bucket is prompt_tokens - cache_read_tokens and the hit bucket is
        cache_read_tokens — the sum stays equal to the upstream input count.
        account_type ('api'|'plan') is mirrored into account_types so the
        cost triggers can zero out real cost for plan accounts.
        """
        conn = self._connect()
        total = 0
        try:
            if account_type not in ("api", "plan"):
                account_type = "api"
            conn.execute(
                "INSERT OR REPLACE INTO account_types (account_name, account_type) "
                "VALUES (?,?)",
                (account_name, account_type),
            )
            if completion_tokens > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,'output',?,?)""",
                    (date, model, account_name, completion_tokens, account_name),
                )
                total += 1
            if cache_read_tokens > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,'input_cache_hit',?,?)""",
                    (date, model, account_name, cache_read_tokens, account_name),
                )
                total += 1
            miss = max(prompt_tokens - cache_read_tokens, 0)
            if miss > 0:
                conn.execute(
                    """INSERT OR REPLACE INTO token_usage
                       (date, model, api_key_name, token_type, amount, cost_group_key)
                       VALUES (?,?,?,'input_cache_miss',?,?)""",
                    (date, model, account_name, miss, account_name),
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

    def upsert_plan_summary(self, month: str, account_name: str,
                            subscription_cost: float, virtual_cost: float):
        """Record one plan account's monthly economics (used on export).

        month is 'YYYY-MM'. subscription_cost is the plan's monthly price
        (present only for months that actually used the plan); virtual_cost
        is the api-billed amount of that usage.
        """
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO proxy_plan_summary "
                "(month, account_name, subscription_cost, virtual_cost) "
                "VALUES (?,?,?,?)",
                (month, account_name, subscription_cost, virtual_cost),
            )
            conn.commit()
        finally:
            conn.close()

    def clear_plan_summary(self):
        """Remove all plan summary rows (called before a full rewrite)."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM proxy_plan_summary")
            conn.commit()
        finally:
            conn.close()

    # ── Load to IR lists ──────────────────────────────────────────────

    def load_to_ir(self):
        conn = self._connect()
        try:
            token_usages: list[TokenUsage] = []
            request_usages: list[RequestUsage] = []
            cost_entries: list[CostEntry] = []
            plan_summary: list[dict] = []
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

            for row in conn.execute(
                "SELECT month, account_name, subscription_cost, virtual_cost "
                "FROM proxy_plan_summary ORDER BY month, account_name"
            ):
                plan_summary.append({
                    "month": row["month"],
                    "account_name": row["account_name"],
                    "subscription_cost": row["subscription_cost"],
                    "virtual_cost": row["virtual_cost"],
                })

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
                plan_summary,
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
        if len(date_str) == 8:  # YYYYMMDD
            return int(date_str[:4]), int(date_str[4:6])
    except (ValueError, IndexError):
        pass
    return 0, 0
