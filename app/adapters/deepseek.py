"""DeepSeek CSV adapter.

Parses DeepSeek's amount-*.csv and cost-*.csv files into IR types.

amount-*.csv columns:
    user_id, utc_date, model, api_key_name, api_key, type, price, amount
    - type=output_tokens           → TokenUsage(token_type="output")
    - type=input_cache_hit_tokens  → TokenUsage(token_type="input_cache_hit")
    - type=input_cache_miss_tokens → TokenUsage(token_type="input_cache_miss")
    - type=request_count           → RequestUsage

cost-*.csv columns:
    user_id, utc_date, model, wallet_type, cost, currency
    → CostEntry(cost_group_key=user_id)
"""

import csv

from app.adapters import register_adapter
from app.data_loader import safe_float, safe_int
from app.ir import CostEntry, RequestUsage, TokenUsage


@register_adapter
class DeepSeekAdapter:
    """Adapter for DeepSeek platform CSV files."""

    platform = "deepseek"

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _norm_date(raw: str) -> str:
        """Normalize a date string to YYYY-MM-DD."""
        raw = raw.strip()
        if len(raw) == 8 and raw.isdigit():  # YYYYMMDD
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        return raw  # already YYYY-MM-DD or unrecognised

    # ── public API ──────────────────────────────────────────────────────

    def parse(self, filepath: str):
        """Parse a DeepSeek CSV file into IR records."""
        token_usages: list[TokenUsage] = []
        request_usages: list[RequestUsage] = []
        cost_entries: list[CostEntry] = []
        year, month = 0, 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = {k.strip().lstrip('﻿'): v.strip() for k, v in row.items()}
                    date = self._norm_date(row.get("utc_date", ""))
                    # Derive year/month from first data row
                    if year == 0:
                        parts = date.split("-")
                        if len(parts) >= 2:
                            try:
                                year = int(parts[0])
                                month = int(parts[1])
                            except ValueError:
                                pass
                    if "type" in row:
                        self._parse_amount_row(row, date, token_usages, request_usages)
                    elif "wallet_type" in row:
                        self._parse_cost_row(row, date, cost_entries)
        except Exception as e:
            print(f"[ERROR] Failed to parse {filepath}: {e}")
            return [], [], [], 0, 0

        return token_usages, request_usages, cost_entries, year, month

    # ── row-level helpers ───────────────────────────────────────────────

    def _parse_amount_row(self, row: dict, date: str,
                          token_usages: list, request_usages: list):
        """Convert a single amount-*.csv row into IR record(s)."""
        rtype = row.get("type", "")
        model = row.get("model", "unknown")
        key_name = row.get("api_key_name", "")

        if rtype == "request_count":
            request_usages.append(RequestUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=key_name,
                count=safe_int(row.get("amount", 0)),
            ))
        elif rtype in ("output_tokens", "input_cache_hit_tokens",
                       "input_cache_miss_tokens"):
            token_type_map = {
                "output_tokens": "output",
                "input_cache_hit_tokens": "input_cache_hit",
                "input_cache_miss_tokens": "input_cache_miss",
            }
            token_usages.append(TokenUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=key_name,
                token_type=token_type_map[rtype],
                amount=safe_int(row.get("amount", 0)),
                cost_group_key=row.get("user_id", ""),
            ))

    def _parse_cost_row(self, row: dict, date: str, cost_entries: list):
        """Convert a single cost-*.csv row into a CostEntry."""
        cost_entries.append(CostEntry(
            platform=self.platform,
            date=date,
            model=row.get("model", "unknown"),
            cost=safe_float(row.get("cost", 0)),
            cost_group_key=row.get("user_id", ""),
        ))
