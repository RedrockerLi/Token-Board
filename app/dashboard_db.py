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
        # Schema is owned by versioned migrations (schema/dashboard/*.sql);
        # apply once at construction. Fails fast (caller aborts) on error.
        from app.migrations import migrate, schema_dir_for
        migrate(self.db_path, schema_dir_for(self.db_path, "dashboard"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

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
                          cost: float = 0.0,
                          account_type: str = "api") -> int:
        """Insert proxy usage data, including a frozen cost_entry row.

        cost is the per-request cost already computed by the proxy (frozen at
        write time, peak/valley-aware) summed over the exported period — it is
        written straight into cost_entry(source='proxy') and never recomputed
        from model_pricing.

        prompt_tokens is the total input (including cache hits), so the miss
        bucket is prompt_tokens - cache_read_tokens and the hit bucket is
        cache_read_tokens — the sum stays equal to the upstream input count.
        account_type ('api'|'plan') is mirrored into account_types (plan
        accounts get cost 0 in cost_entry, subscription + virtual cost live in
        proxy_plan_summary).
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
            # Frozen cost: write directly (plan accounts already carry cost 0
            # from the proxy, so cost_entry stays 0 for them).
            conn.execute(
                """INSERT OR REPLACE INTO cost_entry
                   (date, model, cost, cost_group_key, source)
                   VALUES (?,?,?,?,'proxy')""",
                (date, model, cost, account_name),
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
            # User sort: most-recent call month desc → that month's call volume desc.
            # ym = year*100+month (sortable). request counts are the primary
            # volume signal; token amounts are a fallback for users without
            # request_usage rows.
            token_last_month: dict[str, int] = {}
            token_month_vol: dict[str, int] = {}
            request_last_month: dict[str, int] = {}
            request_month_vol: dict[str, int] = {}

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
                _track_recency(token_last_month, token_month_vol,
                               row["api_key_name"], y, m, row["amount"])

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
                _track_recency(request_last_month, request_month_vol,
                               row["api_key_name"], y, m, row["count"])

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

            def user_sort_key(name: str):
                # Most-recent call month (desc); within the same month, that
                # month's call volume (desc); then name as a stable tiebreak.
                rym = request_last_month.get(name, -1)
                tym = token_last_month.get(name, -1)
                ym = rym if rym >= 0 else tym
                vol = (request_month_vol.get(name, 0) if rym >= 0
                       else token_month_vol.get(name, 0))
                return (-ym, -vol, name.lower())

            return (
                token_usages,
                request_usages,
                cost_entries,
                available_months,
                sorted(api_key_names_set, key=user_sort_key),
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


def _track_recency(last_month: dict, month_vol: dict,
                   name: str, y: int, m: int, volume: int) -> None:
    """Track a user's most-recent month and that month's accumulated volume.

    ym = year*100+month (sortable).  When a later month is seen, the volume
    restarts for that month; equal months accumulate.
    """
    ym = y * 100 + m
    prev = last_month.get(name)
    if prev is None or ym > prev:
        last_month[name] = ym
        month_vol[name] = volume
    elif ym == prev:
        month_vol[name] = month_vol.get(name, 0) + volume
