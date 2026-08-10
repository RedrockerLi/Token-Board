"""DashboardReaderMixin implementation."""

from app.db.dashboard.common import *  # noqa: F401,F403


class DashboardReaderMixin:
    def load_rows(self):
        conn = self._connect()
        try:
            return self._load_v1_rows(conn)
        finally:
            conn.close()

    def get_record_count(self) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT COUNT(*) FROM daily_usage").fetchone()[0]
            recurring = conn.execute(
                "SELECT COUNT(*) FROM monthly_recurring_costs").fetchone()[0]
            return {"daily_usage": rows, "monthly_recurring_costs": recurring}
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
            # V1 keeps the two cost meanings separately. `cost` is the
            # actual metered amount; `theoretical_cost` is the historical
            # pricing equivalent. Consumers must never add these values.
            cost_entries.append({
                **base,
                "cost": row["billed_usage_cost"],
                "theoretical_cost": row["equivalent_cost"],
            })
            months_set.add((y, m)); names.add(name); models.add(row["model"])
            _track_recency(last_month, month_volume, name, y, m, row["request_count"])
        for row in conn.execute(
            "SELECT p.month,p.account_id,COALESCE(a.name,'unknown') account_name,"
            "SUM(CASE WHEN p.normalized_recurring_cost IS NOT NULL "
            "THEN p.normalized_recurring_cost ELSE 0 END) subscription_cost,"
            "SUM(p.equivalent_cost) virtual_cost,"
            "SUM(CASE WHEN p.normalized_recurring_cost IS NULL THEN 1 ELSE 0 END) "
            "billing_incomplete_count FROM monthly_recurring_costs p "
            "LEFT JOIN accounts a ON a.account_id=p.account_id "
            "GROUP BY p.month,p.account_id,a.name ORDER BY p.month,p.account_id"):
            plan_summary.append(dict(row))
            # A recurring charge may exist in a month with no metered
            # traffic. Keep that month visible to /api/monthly instead of
            # deriving the calendar solely from daily_usage rows.
            year, month = _parse_date(f"{row['month']}-01")
            if year:
                months_set.add((year, month))
        available = [{"year": y, "month": m, "label": f"{y}-{m:02d}"}
                     for y, m in sorted(months_set)]
        ordered_names = sorted(names, key=lambda name: (
            -last_month.get(name, -1), -month_volume.get(name, 0), name.lower()))
        return (token_usages, request_usages, cost_entries, available,
                ordered_names, [], _sort_models(models), plan_summary)
