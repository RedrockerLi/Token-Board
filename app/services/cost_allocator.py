"""Proportional cost allocation for API key usage.

When costs are shared across multiple api_key_names in the same
(cost_group_key, date, model) group, these functions split the cost
proportionally based on each key's token consumption.

Rows are plain dicts (one per dashboard.db row) — see DashboardDatabase.load_rows.
"""

from collections import defaultdict


def _compute_shares(token_usages: list[dict], api_key_name: str) -> dict:
    """Per-(cost_group_key, date, model) share of *api_key_name*'s tokens."""
    group_tokens = defaultdict(lambda: defaultdict(int))
    for tu in token_usages:
        gk = (tu["cost_group_key"], tu["date"], tu["model"])
        group_tokens[gk][tu["api_key_name"]] += tu["amount"]

    share = {}
    for gk, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[gk] = selected_tokens / total if total > 0 else 0.0
    return share


def compute_proportional_cost(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> float:
    """Allocate costs to *api_key_name* proportionally by token usage.

    For each (cost_group_key, date, model) group, the cost is split across
    api_key_names based on their share of total tokens.

    Returns:
        float: Rounded to 4 decimal places.
    """
    share = _compute_shares(token_usages, api_key_name)
    total_cost = 0.0
    for ce in cost_entries:
        fraction = share.get((ce["cost_group_key"], ce["date"], ce["model"]), 0.0)
        total_cost += ce["cost"] * fraction
    return round(total_cost, 4)


def compute_proportional_cost_by_model(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> dict[str, float]:
    """Allocate costs to *api_key_name*, broken down by model.

    Returns:
        dict: {model_name: cost_float}
    """
    share = _compute_shares(token_usages, api_key_name)
    model_cost = defaultdict(float)
    for ce in cost_entries:
        fraction = share.get((ce["cost_group_key"], ce["date"], ce["model"]), 0.0)
        model_cost[ce["model"]] += ce["cost"] * fraction
    return {m: round(c, 4) for m, c in model_cost.items()}


def compute_proportional_cost_by_month(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> tuple[dict, dict]:
    """Allocate costs by month; keys are (year, month) tuples.

    Returns RAW floats — callers round at JSON output. These by-period
    variants deliberately cover ALL cost entries (not just non-plan
    accounts); the caller decides which cost rows to pass in.

    Returns:
        (cost_by_month, cost_by_month_by_model)
    """
    share = _compute_shares(token_usages, api_key_name)
    cost = defaultdict(float)
    cost_by_model = defaultdict(lambda: defaultdict(float))
    for ce in cost_entries:
        key = (ce["_year"], ce["_month"])
        fraction = share.get((ce["cost_group_key"], ce["date"], ce["model"]), 0.0)
        cost[key] += ce["cost"] * fraction
        cost_by_model[key][ce["model"]] += ce["cost"] * fraction
    return cost, cost_by_model


def compute_proportional_cost_by_day(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> tuple[dict, dict]:
    """Allocate costs by day; keys are "YYYY-MM-DD" strings.

    Returns RAW floats — callers round at JSON output.

    Returns:
        (cost_by_day, cost_by_day_by_model)
    """
    share = _compute_shares(token_usages, api_key_name)
    cost = defaultdict(float)
    cost_by_model = defaultdict(lambda: defaultdict(float))
    for ce in cost_entries:
        key = ce["date"]
        fraction = share.get((ce["cost_group_key"], ce["date"], ce["model"]), 0.0)
        cost[key] += ce["cost"] * fraction
        cost_by_model[key][ce["model"]] += ce["cost"] * fraction
    return cost, cost_by_model
