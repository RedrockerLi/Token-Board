"""Compatibility helpers for the V1 dashboard cost ledger.

V0 allocated shared cost buckets by token share. V1 stores stable account
identity on every ``daily_usage`` row, so allocation is unnecessary and can
double count a row. The historical function names remain import-compatible,
but now select the already-attributed canonical value from each row.
"""

from collections import defaultdict


def _selected_rows(cost_entries: list[dict], api_key_name: str):
    """Return rows already attributed to the requested V1 account."""
    return (row for row in cost_entries
            if row.get("api_key_name") == api_key_name)


def compute_proportional_cost(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> float:
    """Return actual usage cost already attributed to one V1 account."""
    del token_usages
    return round(sum(float(row.get("cost", 0) or 0)
                     for row in _selected_rows(cost_entries, api_key_name)), 4)


def compute_proportional_cost_by_model(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> dict[str, float]:
    """Allocate costs to *api_key_name*, broken down by model.

    Returns:
        dict: {model_name: cost_float}
    """
    del token_usages
    model_cost = defaultdict(float)
    for ce in _selected_rows(cost_entries, api_key_name):
        model_cost[ce["model"]] += float(ce.get("cost", 0) or 0)
    return {m: round(c, 4) for m, c in model_cost.items()}


def compute_proportional_cost_by_month(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> tuple[dict, dict]:
    """Return actual costs by month; keys are (year, month) tuples.

    Rows are already attributed by the V1 dashboard reader; no proportional
    allocation occurs.

    Returns:
        (cost_by_month, cost_by_month_by_model)
    """
    del token_usages
    cost = defaultdict(float)
    cost_by_model = defaultdict(lambda: defaultdict(float))
    for ce in _selected_rows(cost_entries, api_key_name):
        key = (ce["_year"], ce["_month"])
        value = float(ce.get("cost", 0) or 0)
        cost[key] += value
        cost_by_model[key][ce["model"]] += value
    return cost, cost_by_model


def compute_proportional_cost_by_day(
    token_usages: list[dict],
    cost_entries: list[dict],
    api_key_name: str,
) -> tuple[dict, dict]:
    """Return actual costs by day; keys are "YYYY-MM-DD" strings.

    Returns RAW floats — callers round at JSON output.

    Returns:
        (cost_by_day, cost_by_day_by_model)
    """
    del token_usages
    cost = defaultdict(float)
    cost_by_model = defaultdict(lambda: defaultdict(float))
    for ce in _selected_rows(cost_entries, api_key_name):
        key = ce["date"]
        value = float(ce.get("cost", 0) or 0)
        cost[key] += value
        cost_by_model[key][ce["model"]] += value
    return cost, cost_by_model
