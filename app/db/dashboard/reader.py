"""Read operations for the two-table Dashboard archive."""

from __future__ import annotations

import sqlite3

from app.db.dashboard.common import _parse_date, _sort_models, _track_recency


class DashboardReaderMixin:
    def get_account_ids_by_name(self, name: str) -> list[int]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id FROM users WHERE name=? ORDER BY id", (name,)
            ).fetchall()
            return [int(row["id"]) for row in rows]
        finally:
            conn.close()

    def get_user_ids(self) -> list[int]:
        conn = self._connect()
        try:
            return [int(row["id"]) for row in conn.execute(
                "SELECT id FROM users ORDER BY id").fetchall()]
        finally:
            conn.close()

    def load_rows(self):
        conn = self._connect()
        try:
            return self._load_v2_rows(conn)
        finally:
            conn.close()

    def get_record_count(self) -> dict:
        conn = self._connect()
        try:
            return {
                "daily_model_usage": conn.execute(
                    "SELECT COUNT(*) FROM daily_model_usage").fetchone()[0],
                "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            }
        finally:
            conn.close()

    def _load_v2_rows(self, conn: sqlite3.Connection):
        token_usages, request_usages, cost_entries = [], [], []
        months_set, names, models = set(), set(), set()
        last_month, month_volume = {}, {}
        users = []
        actual_cost_by_user = {}

        for row in conn.execute(
            "SELECT id,name,actual_cost_micro_cny FROM users ORDER BY id"):
            user_id = int(row["id"])
            users.append({"id": user_id, "name": row["name"]})
            actual_cost_by_user[user_id] = int(row["actual_cost_micro_cny"] or 0) / 1_000_000

        for row in conn.execute(
            """SELECT d.*,u.name AS display_name
                 FROM daily_model_usage d JOIN users u ON u.id=d.user_id
                ORDER BY d.usage_date,d.user_id,d.model"""
        ):
            year, month = _parse_date(row["usage_date"])
            if not year:
                continue
            user_id = int(row["user_id"])
            name = row["display_name"]
            base = {
                "platform": "", "source_kind": "proxy",
                "date": row["usage_date"], "model": row["model"],
                "user_id": user_id, "api_key_name": name,
                "cost_group_key": str(user_id), "_year": year, "_month": month,
            }
            miss = max(int(row["input_tokens"]) - int(row["cache_read_tokens"]), 0)
            for token_type, amount in (
                ("input_cache_miss", miss),
                ("input_cache_hit", int(row["cache_read_tokens"])),
                ("output", int(row["output_tokens"])),
            ):
                if amount:
                    token_usages.append({**base, "token_type": token_type, "amount": amount})
            request_usages.append({**base, "count": int(row["request_count"])})
            equivalent = int(row["api_equivalent_cost_micro_cny"] or 0) / 1_000_000
            cost_entries.append({
                **base, "cost": equivalent, "theoretical_cost": equivalent,
                "actual_cost": 0.0,
            })
            months_set.add((year, month))
            names.add(name)
            models.add(row["model"])
            _track_recency(last_month, month_volume, user_id, year, month,
                           int(row["request_count"]))

        available = [
            {"year": year, "month": month, "label": f"{year}-{month:02d}"}
            for year, month in sorted(months_set)
        ]
        ordered_users = sorted(
            users,
            key=lambda user: (
                user["id"] == 0,
                -last_month.get(user["id"], -1),
                -month_volume.get(user["id"], 0),
                str(user["name"]).lower(),
                user["id"],
            ),
        )
        ordered_names = [user["name"] for user in ordered_users]
        return (
            token_usages, request_usages, cost_entries, available,
            ordered_names, [], _sort_models(models), [], ordered_users,
            actual_cost_by_user,
        )
