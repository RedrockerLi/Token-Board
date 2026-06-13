"""Mimo CSV adapter.

Parses Mimo's usage_data_*.csv files into IR types.

usage_data_YYYY-M.csv columns:
    Date, Model, API Key, Currency, Consumed Amount,
    Input Hit Amount, Input Miss Amount, Output Amount,
    Total Tokens, Input Hit Tokens, Input Miss Tokens, Output Tokens,
    Total audio duration, Request Count

A single CSV contains both token and cost data, so each row yields
TokenUsage records AND a CostEntry (plus optionally a RequestUsage).
"""

import csv
import os
import re

from app.adapters import register_adapter
from app.data_loader import safe_float, safe_int
from app.ir import CostEntry, RequestUsage, TokenUsage


@register_adapter
class MimoAdapter:
    """Adapter for Mimo platform CSV files."""

    platform = "mimo"

    # Matches "usage_data_2026-6.csv" or "usage_data_2026-12.csv"
    _FILENAME_RE = re.compile(r"^usage_data_(\d{4})-(\d{1,2})\.csv$")

    # ── public API ──────────────────────────────────────────────────────

    def parse(self, filepath: str):
        """Parse a Mimo CSV file into IR records.

        Returns:
            (token_usages, request_usages, cost_entries, year, month)
        """
        fname = os.path.basename(filepath)
        m = self._FILENAME_RE.match(fname)
        if not m:
            return [], [], [], 0, 0

        year = int(m.group(1))
        month = int(m.group(2))

        token_usages: list[TokenUsage] = []
        request_usages: list[RequestUsage] = []
        cost_entries: list[CostEntry] = []

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row = {k.strip().lstrip('﻿'): v.strip()
                           for k, v in row.items()}
                    self._parse_row(row, token_usages, request_usages,
                                    cost_entries)
        except Exception as e:
            print(f"[ERROR] Failed to parse {filepath}: {e}")
            return [], [], [], 0, 0

        return token_usages, request_usages, cost_entries, year, month

    # ── row-level helpers ───────────────────────────────────────────────

    def _parse_row(self, row: dict,
                   token_usages: list, request_usages: list,
                   cost_entries: list):
        """Convert a single usage_data row into IR records.

        Uses the masked API Key as both api_key_name and cost_group_key
        (Mimo CSVs lack a separate user_id column).
        """
        date = row.get("Date", "")
        model = row.get("Model", "unknown")
        api_key = row.get("API Key", "")

        # ── TokenUsage ────────────────────────────────────────────

        output_tokens = safe_int(row.get("Output Tokens", 0))
        if output_tokens > 0:
            token_usages.append(TokenUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=api_key,
                token_type="output",
                amount=output_tokens,
                cost_group_key=api_key,
            ))

        input_hit_tokens = safe_int(row.get("Input Hit Tokens", 0))
        if input_hit_tokens > 0:
            token_usages.append(TokenUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=api_key,
                token_type="input_cache_hit",
                amount=input_hit_tokens,
                cost_group_key=api_key,
            ))

        input_miss_tokens = safe_int(row.get("Input Miss Tokens", 0))
        if input_miss_tokens > 0:
            token_usages.append(TokenUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=api_key,
                token_type="input_cache_miss",
                amount=input_miss_tokens,
                cost_group_key=api_key,
            ))

        # ── RequestUsage ──────────────────────────────────────────

        request_count = safe_int(row.get("Request Count", 0))
        if request_count > 0:
            request_usages.append(RequestUsage(
                platform=self.platform,
                date=date,
                model=model,
                api_key_name=api_key,
                count=request_count,
            ))

        # ── CostEntry ─────────────────────────────────────────────

        consumed = safe_float(row.get("Consumed Amount", 0))
        if consumed > 0:
            cost_entries.append(CostEntry(
                platform=self.platform,
                date=date,
                model=model,
                cost=consumed,
                cost_group_key=api_key,
            ))
