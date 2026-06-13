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
import re

from app.adapters import register_adapter
from app.data_loader import safe_float, safe_int
from app.ir import CostEntry, RequestUsage, TokenUsage


@register_adapter
class DeepSeekAdapter:
    """Adapter for DeepSeek platform CSV files."""

    platform = "deepseek"

    # Matches "amount-2026-5.csv" or "cost-2026-12.csv"
    _FILENAME_RE = re.compile(r"^(amount|cost)-(\d{4})-(\d{1,2})\.csv$")

    # ── public API ──────────────────────────────────────────────────────

    def parse(self, filepath: str):

        """Parse a DeepSeek CSV file into IR records.

        Returns:
            (token_usages: list[TokenUsage],
             request_usages: list[RequestUsage],
             cost_entries: list[CostEntry],
             year: int,
             month: int)
        """
        import os
        fname = os.path.basename(filepath)
        m = self._FILENAME_RE.match(fname)
        if not m:
            return [], [], [], 0, 0

        csv_type = m.group(1)
        year = int(m.group(2))
        month = int(m.group(3))

        token_usages: list[TokenUsage] = []
        request_usages: list[RequestUsage] = []
        cost_entries: list[CostEntry] = []

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = {k.strip().lstrip('﻿'): v.strip() for k, v in row.items()}
                    if csv_type == "amount":
                        self._parse_amount_row(row, token_usages, request_usages)
                    else:  # csv_type == "cost"
                        self._parse_cost_row(row, cost_entries)
        except Exception as e:
            print(f"[ERROR] Failed to parse {filepath}: {e}")
            return [], [], [], 0, 0

        return token_usages, request_usages, cost_entries, year, month

    # ── row-level helpers ───────────────────────────────────────────────

    def _parse_amount_row(self, row: dict,
                          token_usages: list, request_usages: list):
        """Convert a single amount-*.csv row into IR record(s)."""
        rtype = row.get("type", "")
        date = row.get("utc_date", "")
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

    def _parse_cost_row(self, row: dict, cost_entries: list):
        """Convert a single cost-*.csv row into a CostEntry."""
        cost_entries.append(CostEntry(
            platform=self.platform,
            date=row.get("utc_date", ""),
            model=row.get("model", "unknown"),
            cost=safe_float(row.get("cost", 0)),
            cost_group_key=row.get("user_id", ""),
        ))
