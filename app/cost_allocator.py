"""Proportional cost allocation for API key usage.

When costs are shared across multiple api_key_names in the same
(cost_group_key, date, model) group, these functions split the cost
proportionally based on each key's token consumption.
"""

from collections import defaultdict

from app.ir import CostEntry, TokenUsage


def compute_proportional_cost(
    token_usages: list[TokenUsage],
    cost_entries: list[CostEntry],
    api_key_name: str,
) -> float:
    """Allocate costs to *api_key_name* proportionally by token usage.

    For each (cost_group_key, date, model) group, the cost is split across
    api_key_names based on their share of total tokens.

    Returns:
        float: Rounded to 4 decimal places.
    """
    # Step 1: total tokens per (cost_group_key, date, model, api_key_name)
    group_tokens = defaultdict(lambda: defaultdict(int))

    for tu in token_usages:
        gk = (tu.cost_group_key, tu.date, tu.model)
        group_tokens[gk][tu.api_key_name] += tu.amount

    # Step 2: share of selected api_key_name per group
    share = {}
    for gk, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[gk] = selected_tokens / total if total > 0 else 0.0

    # Step 3: allocate cost records proportionally
    total_cost = 0.0
    for ce in cost_entries:
        fraction = share.get((ce.cost_group_key, ce.date, ce.model), 0.0)
        total_cost += ce.cost * fraction

    return round(total_cost, 4)


def compute_proportional_cost_by_model(
    token_usages: list[TokenUsage],
    cost_entries: list[CostEntry],
    api_key_name: str,
) -> dict[str, float]:
    """Allocate costs to *api_key_name*, broken down by model.

    Returns:
        dict: {model_name: cost_float}
    """
    # Step 1: total tokens per (cost_group_key, date, model, api_key_name)
    group_tokens = defaultdict(lambda: defaultdict(int))

    for tu in token_usages:
        gk = (tu.cost_group_key, tu.date, tu.model)
        group_tokens[gk][tu.api_key_name] += tu.amount

    # Step 2: share of selected api_key_name per group
    share = {}
    for gk, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[gk] = selected_tokens / total if total > 0 else 0.0

    # Step 3: allocate cost records proportionally, by model
    model_cost = defaultdict(float)
    for ce in cost_entries:
        fraction = share.get((ce.cost_group_key, ce.date, ce.model), 0.0)
        model_cost[ce.model] += ce.cost * fraction

    return {m: round(c, 4) for m, c in model_cost.items()}
