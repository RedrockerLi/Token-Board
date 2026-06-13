"""Intermediate Representation (IR) data models.

Platform-agnostic dataclasses that decouple the CSV parsing layer
from the business logic and API responses. Every adapter writes into
these types so that routes and allocators never see raw CSV rows.
"""

from dataclasses import dataclass


@dataclass
class TokenUsage:
    """A single day's token consumption of a particular type."""
    platform: str
    date: str              # "YYYY-MM-DD"
    model: str
    api_key_name: str
    token_type: str        # "output" | "input_cache_hit" | "input_cache_miss"
    amount: int
    cost_group_key: str = ""  # user_id, for proportional cost matching

    # Source-file metadata (set by the loader, NOT the adapter)
    _year: int = 0
    _month: int = 0


@dataclass
class RequestUsage:
    """A single day's API request count."""
    platform: str
    date: str              # "YYYY-MM-DD"
    model: str
    api_key_name: str
    count: int

    _year: int = 0
    _month: int = 0


@dataclass
class CostEntry:
    """A single day's cost record — used for proportional allocation."""
    platform: str
    date: str              # "YYYY-MM-DD"
    model: str
    cost: float
    cost_group_key: str    # user_id — pairing key for proportional matching

    _year: int = 0
    _month: int = 0
