"""Dashboard SQLite database — storage for #/dashboard visualization data.

Stores daily-aggregated usage records with indexes for fast queries.
"""

import sqlite3


# Model display order — lower number = first.  Models not listed default to 99.
MODEL_ORDER = {
    "deepseek-v4-flash": 1,
    "deepseek-v4-pro": 2,
    "mimo-v2.5": 3,
    "Qwen3.5-397B-A17B": 4,
}


def _is_v1(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_usage'"
    ).fetchone() is not None


class DashboardDatabase:
    """SQLite-backed storage for dashboard visualization data."""

    def __init__(self, db_path: str, schema_dir: str | None = None):
        self.db_path = db_path
        # Schema is owned by versioned migrations (schema/dashboard/vN/*.sql);
        # apply once at construction. Fails fast (caller aborts) on error.
        # schema_dir may be passed explicitly when db_path is a shadow/temp
        # copy outside the standard data/ layout (schema_dir_for would mis-derive).
        from app.db.migrations import migrate, schema_dir_for
        migrate(self.db_path, schema_dir or schema_dir_for(self.db_path, "dashboard"),
                "dashboard")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── Proxy export writes (additive, exactly-once) ────────────────────

    def upsert_proxy_data(self, date: str, model: str,
                          account_id: int, prompt_tokens: int,
                          completion_tokens: int, cache_read_tokens: int,
                          request_count: int,
                          cost: float = 0.0,
                          billed_usage_cost: float | None = None) -> int:
        """Insert proxy usage data, including a frozen cost_entry row.

        cost is the per-request cost already computed by the proxy (frozen at
        write time, peak/valley-aware) summed over the exported period — it is
        written straight into cost_entry and never recomputed.

        Buckets are keyed by account_id (the stable identity; the display name
        lives in the `accounts` mirror). Writes are ADDITIVE (ON CONFLICT ...
        DO UPDATE ... +=): each export batch contributes exactly once, so
        partial/re-export never double-counts and an older merge never regresses
        a value. token_usage buckets: miss = prompt - cache_read, hit =
        cache_read (sum = upstream input count).
        """
        conn = self._connect()
        total = 0
        try:
            if _is_v1(conn):
                billed = cost if billed_usage_cost is None else billed_usage_cost
                conn.execute(
                    """INSERT INTO daily_usage
                       (date,account_id,model,input_tokens,cache_tokens,output_tokens,
                        request_count,equivalent_cost,billed_usage_cost)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(date,account_id,model) DO UPDATE SET
                         input_tokens=daily_usage.input_tokens+excluded.input_tokens,
                         cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
                         output_tokens=daily_usage.output_tokens+excluded.output_tokens,
                         request_count=daily_usage.request_count+excluded.request_count,
                         equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
                         billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost""",
                    (date, account_id, model, prompt_tokens, cache_read_tokens,
                     completion_tokens, request_count, cost, billed),
                )
                conn.commit()
                return 1
            if completion_tokens > 0:
                conn.execute(
                    """INSERT INTO token_usage
                       (date, model, account_id, token_type, amount)
                       VALUES (?,?,?,'output',?)
                       ON CONFLICT(date, model, account_id, token_type)
                       DO UPDATE SET amount = token_usage.amount + excluded.amount""",
                    (date, model, account_id, completion_tokens),
                )
                total += 1
            if cache_read_tokens > 0:
                conn.execute(
                    """INSERT INTO token_usage
                       (date, model, account_id, token_type, amount)
                       VALUES (?,?,?,'input_cache_hit',?)
                       ON CONFLICT(date, model, account_id, token_type)
                       DO UPDATE SET amount = token_usage.amount + excluded.amount""",
                    (date, model, account_id, cache_read_tokens),
                )
                total += 1
            miss = max(prompt_tokens - cache_read_tokens, 0)
            if miss > 0:
                conn.execute(
                    """INSERT INTO token_usage
                       (date, model, account_id, token_type, amount)
                       VALUES (?,?,?,'input_cache_miss',?)
                       ON CONFLICT(date, model, account_id, token_type)
                       DO UPDATE SET amount = token_usage.amount + excluded.amount""",
                    (date, model, account_id, miss),
                )
                total += 1
            if request_count > 0:
                conn.execute(
                    """INSERT INTO request_usage
                       (date, model, account_id, count)
                       VALUES (?,?,?,?)
                       ON CONFLICT(date, model, account_id)
                       DO UPDATE SET count = request_usage.count + excluded.count""",
                    (date, model, account_id, request_count),
                )
                total += 1
            # Frozen cost: additive. `cost` here is the api-equivalent price for
            # EVERY account (api real bill and plan's theoretical bill alike —
            # the proxy writes one unified api_cost column). Plan accounts'
            # real cost lives in proxy_plan_summary.subscription_cost; their
            # api-equivalent in cost_entry is used by the usage cards.
            conn.execute(
                """INSERT INTO cost_entry
                   (date, model, cost, account_id)
                   VALUES (?,?,?,?)
                   ON CONFLICT(date, model, account_id)
                   DO UPDATE SET cost = cost_entry.cost + excluded.cost""",
                (date, model, cost, account_id),
            )
            total += 1
            conn.commit()
        finally:
            conn.close()
        return total

    def upsert_proxy_batch(self, rows: list[dict]) -> int:
        """Bulk export using one connection and one transaction."""
        if not rows:
            return 0
        conn = self._connect()
        try:
            if not _is_v1(conn):
                # V0 remains an archive-only compatibility path.
                conn.close()
                return sum(self.upsert_proxy_data(**row) for row in rows)
            conn.executemany(
                """INSERT INTO daily_usage
                   (date,account_id,model,input_tokens,cache_tokens,output_tokens,
                    request_count,equivalent_cost,billed_usage_cost)
                   VALUES(:date,:account_id,:model,:prompt_tokens,:cache_read_tokens,
                          :completion_tokens,:request_count,:cost,:billed_usage_cost)
                   ON CONFLICT(date,account_id,model) DO UPDATE SET
                     input_tokens=daily_usage.input_tokens+excluded.input_tokens,
                     cache_tokens=daily_usage.cache_tokens+excluded.cache_tokens,
                     output_tokens=daily_usage.output_tokens+excluded.output_tokens,
                     request_count=daily_usage.request_count+excluded.request_count,
                     equivalent_cost=daily_usage.equivalent_cost+excluded.equivalent_cost,
                     billed_usage_cost=daily_usage.billed_usage_cost+excluded.billed_usage_cost""",
                rows,
            )
            conn.commit()
            return len(rows)
        finally:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                pass

    def purge_zero_usage_rows(self) -> int:
        """Delete archive rows that carry no real usage.

        A (date, model, account_id) bucket with request/cost rows but NO
        token_usage rows represents failed/aborted or test requests — they are
        recorded with zero tokens (and zero cost) and must not show up as
        empty per-model cards. token_usage rows only exist for positive
        amounts, so "has token rows" == "has real usage"; buckets with any
        token usage (even 0-cost) are always preserved. Returns rows deleted.
        """
        conn = self._connect()
        try:
            if _is_v1(conn):
                deleted = conn.execute(
                    "DELETE FROM daily_usage WHERE input_tokens=0 AND "
                    "cache_tokens=0 AND output_tokens=0 AND equivalent_cost=0"
                ).rowcount
                conn.commit()
                return deleted
            deleted = conn.execute(
                """DELETE FROM request_usage
                   WHERE NOT EXISTS (SELECT 1 FROM token_usage t
                                     WHERE t.date = request_usage.date
                                       AND t.model = request_usage.model
                                       AND t.account_id = request_usage.account_id)"""
            ).rowcount
            deleted += conn.execute(
                """DELETE FROM cost_entry
                   WHERE cost = 0
                     AND NOT EXISTS (SELECT 1 FROM token_usage t
                                     WHERE t.date = cost_entry.date
                                       AND t.model = cost_entry.model
                                       AND t.account_id = cost_entry.account_id)"""
            ).rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    def accumulate_plan_summary(self, month: str, account_id: int, key_masked: str,
                                subscription_cost: float, virtual_cost: float,
                                refresh_subscription: bool = False):
        """Accumulate one plan account's monthly economics into the archive.

        virtual_cost is ADDED to the bucket: request_log rows are cleaned 30d
        after export, so the archive can never be recomputed from logs — it
        accumulates (each export batch contributes once). subscription_cost is
        set on first write and refreshed ONLY while the month is still current
        (refresh_subscription=True), so past months are frozen forever — price
        edits affect only the current month.
        """
        conn = self._connect()
        try:
            if _is_v1(conn):
                conn.execute(
                    """INSERT INTO monthly_recurring_costs
                       (month,account_id,billing_unit_id,recurring_charge,equivalent_cost)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(month,account_id,billing_unit_id) DO UPDATE SET
                         equivalent_cost=monthly_recurring_costs.equivalent_cost+
                                         excluded.equivalent_cost,
                         recurring_charge=CASE WHEN ? THEN excluded.recurring_charge
                           ELSE monthly_recurring_costs.recurring_charge END""",
                    (month, account_id, key_masked, subscription_cost, virtual_cost,
                     bool(refresh_subscription)),
                )
                conn.commit()
                return
            conn.execute(
                """INSERT INTO proxy_plan_summary
                   (month, account_id, key_masked, subscription_cost, virtual_cost)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(month, account_id, key_masked) DO UPDATE SET
                     virtual_cost = proxy_plan_summary.virtual_cost + excluded.virtual_cost,
                     subscription_cost = CASE WHEN ? THEN excluded.subscription_cost
                                              ELSE proxy_plan_summary.subscription_cost END""",
                (month, account_id, key_masked, subscription_cost, virtual_cost,
                 bool(refresh_subscription)),
            )
            conn.commit()
        finally:
            conn.close()

    def reconcile_plan_subscription(self, account_id: int, key_masked: str,
                                    subscriptions: dict[str, float]) -> None:
        """Set one key lifecycle's subscription rows without touching virtual cost.

        Unlike request usage, subscriptions are derived state.  Reconciliation
        lets an edited valid_from, cancellation, or scheduled price remove a
        previously generated period while preserving the additive virtual-cost
        archive in the same row.
        """
        conn = self._connect()
        try:
            if _is_v1(conn):
                existing = conn.execute(
                    "SELECT month FROM monthly_recurring_costs "
                    "WHERE account_id=? AND billing_unit_id=?",
                    (account_id, key_masked),
                ).fetchall()
                for row in existing:
                    if row["month"] not in subscriptions:
                        conn.execute(
                            "UPDATE monthly_recurring_costs SET recurring_charge=0 "
                            "WHERE account_id=? AND billing_unit_id=? AND month=?",
                            (account_id, key_masked, row["month"]),
                        )
                for month, cost in subscriptions.items():
                    conn.execute(
                        "INSERT INTO monthly_recurring_costs"
                        "(month,account_id,billing_unit_id,recurring_charge,equivalent_cost) "
                        "VALUES(?,?,?,?,0) ON CONFLICT(month,account_id,billing_unit_id) "
                        "DO UPDATE SET recurring_charge=excluded.recurring_charge",
                        (month, account_id, key_masked, cost),
                    )
                conn.commit()
                return
            existing = conn.execute(
                "SELECT month FROM proxy_plan_summary WHERE account_id=? AND key_masked=?",
                (account_id, key_masked),
            ).fetchall()
            for row in existing:
                if row["month"] not in subscriptions:
                    conn.execute(
                        "UPDATE proxy_plan_summary SET subscription_cost=0 "
                        "WHERE account_id=? AND key_masked=? AND month=?",
                        (account_id, key_masked, row["month"]),
                    )
            for month, cost in subscriptions.items():
                conn.execute(
                    "INSERT INTO proxy_plan_summary "
                    "(month,account_id,key_masked,subscription_cost,virtual_cost) "
                    "VALUES (?,?,?,?,0) "
                    "ON CONFLICT(month,account_id,key_masked) DO UPDATE SET "
                    "subscription_cost=excluded.subscription_cost",
                    (month, account_id, key_masked, cost),
                )
            conn.commit()
        finally:
            conn.close()

    # ── Load rows ─────────────────────────────────────────────────────

    def load_rows(self):
        conn = self._connect()
        try:
            if _is_v1(conn):
                return self._load_v1_rows(conn)
            token_usages: list[dict] = []
            request_usages: list[dict] = []
            cost_entries: list[dict] = []
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

            for row in conn.execute(
                "SELECT t.*, COALESCE(a.name, 'unknown') AS _display_name "
                "FROM token_usage t LEFT JOIN accounts a ON a.account_id = t.account_id"
            ):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                name = row["_display_name"]
                tu = {
                    "platform": "",
                    "date": date,
                    "model": row["model"],
                    "api_key_name": name,
                    "token_type": row["token_type"],
                    "amount": row["amount"],
                    "cost_group_key": name,
                    "_year": y,
                    "_month": m,
                }
                token_usages.append(tu)
                months_set.add((y, m))
                api_key_names_set.add(name)
                models_set.add(row["model"])
                _track_recency(token_last_month, token_month_vol,
                               name, y, m, row["amount"])

            for row in conn.execute(
                "SELECT r.*, COALESCE(a.name, 'unknown') AS _display_name "
                "FROM request_usage r LEFT JOIN accounts a ON a.account_id = r.account_id"
            ):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                name = row["_display_name"]
                ru = {
                    "platform": "",
                    "date": date,
                    "model": row["model"],
                    "api_key_name": name,
                    "count": row["count"],
                    "_year": y,
                    "_month": m,
                }
                request_usages.append(ru)
                months_set.add((y, m))
                api_key_names_set.add(name)
                models_set.add(row["model"])
                _track_recency(request_last_month, request_month_vol,
                               name, y, m, row["count"])

            for row in conn.execute(
                "SELECT c.*, COALESCE(a.name, 'unknown') AS _display_name "
                "FROM cost_entry c LEFT JOIN accounts a ON a.account_id = c.account_id"
            ):
                date = row["date"]
                y, m = _parse_date(date)
                if y == 0:
                    continue
                name = row["_display_name"]
                ce = {
                    "platform": "",
                    "date": date,
                    "model": row["model"],
                    "cost": row["cost"],
                    "cost_group_key": name,
                    "_year": y,
                    "_month": m,
                }
                cost_entries.append(ce)
                months_set.add((y, m))
                models_set.add(row["model"])

            for row in conn.execute(
                "SELECT p.month, p.account_id, "
                "COALESCE(a.name, 'unknown') AS account_name, "
                "p.subscription_cost, p.virtual_cost "
                "FROM proxy_plan_summary p "
                "LEFT JOIN accounts a ON a.account_id = p.account_id "
                "ORDER BY p.month, p.account_id"
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
            if _is_v1(conn):
                rows = conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0]
                recurring = conn.execute(
                    "SELECT COUNT(*) FROM monthly_recurring_costs").fetchone()[0]
                return {"daily_usage": rows,
                        "monthly_recurring_costs": recurring}
            return {
                "token_usage": conn.execute("SELECT COUNT(*) FROM token_usage").fetchone()[0],
                "request_usage": conn.execute("SELECT COUNT(*) FROM request_usage").fetchone()[0],
                "cost_entry": conn.execute("SELECT COUNT(*) FROM cost_entry").fetchone()[0],
            }
        finally:
            conn.close()

    def _load_v1_rows(self, conn: sqlite3.Connection):
        token_usages, request_usages, cost_entries, plan_summary = [], [], [], []
        months_set, names, models = set(), set(), set()
        last_month, month_volume = {}, {}
        for row in conn.execute(
            "SELECT d.*,COALESCE(a.name,'unknown') AS display_name FROM daily_usage d "
            "LEFT JOIN accounts a ON a.account_id=d.account_id"):
            y, m = _parse_date(row["date"])
            if not y:
                continue
            name = row["display_name"]
            base = {"platform": "", "date": row["date"], "model": row["model"],
                    "api_key_name": name, "cost_group_key": name,
                    "_year": y, "_month": m}
            miss = max(row["input_tokens"] - row["cache_tokens"], 0)
            for token_type, amount in (
                ("input_cache_miss", miss), ("input_cache_hit", row["cache_tokens"]),
                ("output", row["output_tokens"])):
                if amount:
                    token_usages.append({**base, "token_type": token_type, "amount": amount})
            request_usages.append({**base, "count": row["request_count"]})
            cost_entries.append({**base, "cost": row["equivalent_cost"]})
            months_set.add((y, m)); names.add(name); models.add(row["model"])
            _track_recency(last_month, month_volume, name, y, m, row["request_count"])
        for row in conn.execute(
            "SELECT p.month,p.account_id,COALESCE(a.name,'unknown') account_name,"
            "SUM(p.recurring_charge) subscription_cost,"
            "SUM(p.equivalent_cost) virtual_cost FROM monthly_recurring_costs p "
            "LEFT JOIN accounts a ON a.account_id=p.account_id "
            "GROUP BY p.month,p.account_id,a.name ORDER BY p.month,p.account_id"):
            plan_summary.append(dict(row))
        available = [{"year": y, "month": m, "label": f"{y}-{m:02d}"}
                     for y, m in sorted(months_set)]
        ordered_names = sorted(names, key=lambda name: (
            -last_month.get(name, -1), -month_volume.get(name, 0), name.lower()))
        return (token_usages, request_usages, cost_entries, available,
                ordered_names, [], _sort_models(models), plan_summary)


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


# ── Account mirror reconciliation ─────────────────────────────────────────

def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _rebuild_tables_by_id(dash: sqlite3.Connection) -> None:
    """Rebuild the four archive tables keyed by account_id (drop the legacy
    name columns, add account_id unique indexes). Called once by
    reconcile_accounts when legacy name columns are still present."""
    # token_usage
    dash.execute("DROP INDEX IF EXISTS idx_tu_unique")
    dash.execute("DROP INDEX IF EXISTS idx_tu_query")
    dash.execute("DROP TABLE IF EXISTS token_usage_new")
    dash.execute(
        "CREATE TABLE token_usage_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, model TEXT NOT NULL,"
        " account_id INTEGER NOT NULL DEFAULT 0, token_type TEXT NOT NULL, amount INTEGER NOT NULL)")
    dash.execute(
        "INSERT INTO token_usage_new (id, date, model, account_id, token_type, amount) "
        "SELECT id, date, model, account_id, token_type, amount FROM token_usage")
    dash.execute("DROP TABLE token_usage")
    dash.execute("ALTER TABLE token_usage_new RENAME TO token_usage")
    dash.execute(
        "CREATE UNIQUE INDEX idx_tu_unique ON token_usage(date, model, account_id, token_type)")
    dash.execute("CREATE INDEX idx_tu_query ON token_usage(account_id, date, model)")

    # request_usage
    dash.execute("DROP INDEX IF EXISTS idx_ru_unique")
    dash.execute("DROP INDEX IF EXISTS idx_ru_query")
    dash.execute("DROP TABLE IF EXISTS request_usage_new")
    dash.execute(
        "CREATE TABLE request_usage_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, model TEXT NOT NULL,"
        " account_id INTEGER NOT NULL DEFAULT 0, count INTEGER NOT NULL)")
    dash.execute(
        "INSERT INTO request_usage_new (id, date, model, account_id, count) "
        "SELECT id, date, model, account_id, count FROM request_usage")
    dash.execute("DROP TABLE request_usage")
    dash.execute("ALTER TABLE request_usage_new RENAME TO request_usage")
    dash.execute(
        "CREATE UNIQUE INDEX idx_ru_unique ON request_usage(date, model, account_id)")
    dash.execute("CREATE INDEX idx_ru_query ON request_usage(account_id, date, model)")

    # cost_entry
    dash.execute("DROP INDEX IF EXISTS idx_ce_unique")
    dash.execute("DROP INDEX IF EXISTS idx_ce_query")
    dash.execute("DROP TABLE IF EXISTS cost_entry_new")
    dash.execute(
        "CREATE TABLE cost_entry_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, model TEXT NOT NULL,"
        " cost REAL NOT NULL, account_id INTEGER NOT NULL DEFAULT 0)")
    dash.execute(
        "INSERT INTO cost_entry_new (id, date, model, cost, account_id) "
        "SELECT id, date, model, cost, account_id FROM cost_entry")
    dash.execute("DROP TABLE cost_entry")
    dash.execute("ALTER TABLE cost_entry_new RENAME TO cost_entry")
    dash.execute(
        "CREATE UNIQUE INDEX idx_ce_unique ON cost_entry(date, model, account_id)")
    dash.execute("CREATE INDEX idx_ce_query ON cost_entry(account_id, date, model)")

    # proxy_plan_summary (was UNIQUE(month, account_name) inline — rebuilt)
    dash.execute("DROP INDEX IF EXISTS idx_pps_unique")
    dash.execute("DROP TABLE IF EXISTS proxy_plan_summary_new")
    dash.execute(
        "CREATE TABLE proxy_plan_summary_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT NOT NULL,"
        " account_id INTEGER NOT NULL DEFAULT 0,"
        " key_masked TEXT NOT NULL DEFAULT '',"
        " subscription_cost REAL NOT NULL DEFAULT 0, virtual_cost REAL NOT NULL DEFAULT 0)")
    dash.execute(
        "INSERT INTO proxy_plan_summary_new (id, month, account_id, key_masked, subscription_cost, virtual_cost) "
        "SELECT id, month, account_id, '', subscription_cost, virtual_cost FROM proxy_plan_summary")
    dash.execute("DROP TABLE proxy_plan_summary")
    dash.execute("ALTER TABLE proxy_plan_summary_new RENAME TO proxy_plan_summary")
    dash.execute(
        "CREATE UNIQUE INDEX idx_pps_unique ON proxy_plan_summary(month, account_id, key_masked)")


def reconcile_accounts(dash_path: str, proxy_path: str) -> None:
    """Mirror upstream_accounts (id → name) into the dashboard `accounts` table
    and migrate any legacy name-keyed archive rows to account_id keys.
    Idempotent.

    Runs on the local dashboard at startup and on the sync shadow right after
    the schema migrate (before export), so the local archive and the cloud copy
    both converge on account_id bucketing with a consistent name mirror. Once
    the legacy name columns are dropped the rebuild step is skipped.

    Only `name` is mirrored (dashboard 0006 dropped the never-read
    account_type / deleted_at mirror columns); the proxy DB is the
    authoritative account store.
    """
    proxy = sqlite3.connect(proxy_path)
    proxy.row_factory = sqlite3.Row
    dash = sqlite3.connect(dash_path, timeout=10)
    try:
        proxy_v1 = proxy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='upstreams'"
        ).fetchone() is not None
        accts = proxy.execute(
            "SELECT id,name FROM accounts" if proxy_v1
            else "SELECT id,name FROM upstream_accounts"
        ).fetchall()
        for a in accts:
            dash.execute(
                "INSERT OR REPLACE INTO accounts (account_id, name) VALUES (?,?)",
                (a["id"], a["name"]),
            )

        # Legacy name-keyed rows: backfill account_id from the name→id map,
        # then rebuild the tables without the name columns.
        if _is_v1(dash):
            dash.commit()
            return
        name_cols = {
            "token_usage": "api_key_name",
            "request_usage": "api_key_name",
            "cost_entry": "cost_group_key",
            "proxy_plan_summary": "account_name",
        }
        legacy = any(_column_exists(dash, t, c) for t, c in name_cols.items())
        if legacy:
            name2id = {a["name"]: a["id"] for a in accts}
            for table, col in name_cols.items():
                if not _column_exists(dash, table, col):
                    continue
                for name, aid in name2id.items():
                    dash.execute(
                        f"UPDATE {table} SET account_id = ? "
                        f"WHERE account_id = 0 AND {col} = ?",
                        (aid, name),
                    )
            _rebuild_tables_by_id(dash)
        dash.commit()
    finally:
        proxy.close()
        dash.close()
