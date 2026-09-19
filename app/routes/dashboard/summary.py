"""Homepage Dashboard summary API."""

from collections import defaultdict

from app.routes.dashboard.common import bp, jsonify, request, _store


def _selected_user_id():
    raw = request.args.get("user_id", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@bp.route("/api/summary")
def api_summary():
    user_id = _selected_user_id()
    store = _store()
    model_tokens = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0,
    })
    total_output = total_input_hit = total_input_miss = total_requests = 0

    for usage in store.token_usages:
        if user_id is not None and usage["user_id"] != user_id:
            continue
        amount = usage["amount"]
        if usage["token_type"] == "output":
            total_output += amount
            model_tokens[usage["model"]]["output"] += amount
        elif usage["token_type"] == "input_cache_hit":
            total_input_hit += amount
            model_tokens[usage["model"]]["input_hit"] += amount
        else:
            total_input_miss += amount
            model_tokens[usage["model"]]["input_miss"] += amount

    for usage in store.request_usages:
        if user_id is not None and usage["user_id"] != user_id:
            continue
        total_requests += usage["count"]
        model_tokens[usage["model"]]["requests"] += usage["count"]

    selected_costs = [
        entry for entry in store.cost_entries
        if user_id is None or entry["user_id"] == user_id
    ]
    model_cost = defaultdict(float)
    theoretical_total = 0.0
    for entry in selected_costs:
        value = float(entry.get("theoretical_cost", 0) or 0)
        theoretical_total += value
        model_cost[entry["model"]] += value

    model_breakdown = {}
    for model in sorted(set(model_tokens) | set(model_cost)):
        stats = model_tokens[model]
        model_breakdown[model] = {
            "output_tokens": stats["output"],
            "input_cache_hit_tokens": stats["input_hit"],
            "input_cache_miss_tokens": stats["input_miss"],
            "total_tokens": stats["output"] + stats["input_hit"] + stats["input_miss"],
            "requests": stats["requests"],
            "theoretical_cost": round(model_cost.get(model, 0), 6),
            "cost": round(model_cost.get(model, 0), 6),
        }

    if user_id is None:
        actual = sum(store.user_actual_cost.values())
    else:
        actual = store.user_actual_cost.get(user_id, 0.0)

    total_tokens = total_output + total_input_hit + total_input_miss
    return jsonify({
        "total_output_tokens": total_output,
        "total_input_cache_hit_tokens": total_input_hit,
        "total_input_cache_miss_tokens": total_input_miss,
        "total_input_tokens": total_input_hit + total_input_miss,
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "actual_cost": round(actual, 6),
        "theoretical_total_cost": round(theoretical_total, 6),
        "model_breakdown": model_breakdown,
        "users": store.users,
        "models": store.models,
        "available_months": store.available_months,
    })
