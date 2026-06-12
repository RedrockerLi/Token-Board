"""Proportional cost allocation for API key usage.

When costs are shared across multiple api_key_names in the same
(user_id, date, model) group, these functions split the cost
proportionally based on each key's token consumption.
"""

from collections import defaultdict

from app.data_loader import safe_float, safe_int


def compute_proportional_cost(amount_records, cost_records, api_key_name):
    """Allocate costs to a specific api_key_name proportionally by token usage.

    For each (user_id, utc_date, model) group, the cost is split across
    api_key_names based on their share of total tokens in that group.

    Returns:
        float: Rounded to 4 decimal places.
    """
    # Step 1: total tokens per (user_id, date, model, api_key_name)
    group_tokens = defaultdict(lambda: defaultdict(int))

    for r in amount_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        key_name = r.get("api_key_name", "")
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))

        if rtype in ("output_tokens", "input_cache_hit_tokens", "input_cache_miss_tokens"):
            group_tokens[(uid, date, model)][key_name] += amount

    # Step 2: share of selected api_key_name per group
    share = {}
    for group_key, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[group_key] = selected_tokens / total if total > 0 else 0.0

    # Step 3: allocate cost records proportionally
    total_cost = 0.0
    for r in cost_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        cost = safe_float(r.get("cost", 0))
        fraction = share.get((uid, date, model), 0.0)
        total_cost += cost * fraction

    return round(total_cost, 4)


def compute_proportional_cost_by_model(amount_records, cost_records, api_key_name):
    """Allocate costs to a specific api_key_name, broken down by model.

    Returns:
        dict: {model_name: cost_float}
    """
    # Step 1: total tokens per (user_id, date, model, api_key_name)
    group_tokens = defaultdict(lambda: defaultdict(int))

    for r in amount_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        key_name = r.get("api_key_name", "")
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))

        if rtype in ("output_tokens", "input_cache_hit_tokens", "input_cache_miss_tokens"):
            group_tokens[(uid, date, model)][key_name] += amount

    # Step 2: share of selected api_key_name per group
    share = {}
    for group_key, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[group_key] = selected_tokens / total if total > 0 else 0.0

    # Step 3: allocate cost records proportionally, by model
    model_cost = defaultdict(float)
    for r in cost_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        cost = safe_float(r.get("cost", 0))
        fraction = share.get((uid, date, model), 0.0)
        model_cost[model] += cost * fraction

    return {m: round(c, 4) for m, c in model_cost.items()}
