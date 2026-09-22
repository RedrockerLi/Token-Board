"""Homepage Dashboard summary API."""

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from app.routes.dashboard.common import bp, jsonify, request, _store


DECAY_RATE = 0.9
PROJECT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _decay_baseline_month():
    """Return the month used as the weight baseline for this request.

    The Dashboard sends its current natural month explicitly.  Direct API
    callers may omit it; in that case use the project's default timezone so
    the result does not depend on the host process timezone.
    """

    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if year is not None and month is not None and year > 0 and 1 <= month <= 12:
        return year, month

    current = datetime.now(PROJECT_TIMEZONE)
    return current.year, current.month


def _month_distance(baseline_year, baseline_month, usage_year, usage_month):
    """Return elapsed natural months, clamping future records to zero."""

    elapsed = ((baseline_year - usage_year) * 12 +
               (baseline_month - usage_month))
    return max(0, elapsed)


def _token_weight(baseline_year, baseline_month, usage_year, usage_month):
    """Return the unrounded monthly decay weight for one usage month."""

    distance = _month_distance(
        baseline_year, baseline_month, usage_year, usage_month)
    return DECAY_RATE ** distance


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
    baseline_year, baseline_month = _decay_baseline_month()
    user_id = _selected_user_id()
    store = _store()
    model_tokens = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0,
    })
    model_weighted_tokens = defaultdict(float)
    total_output = total_input_hit = total_input_miss = total_requests = 0

    for usage in store.token_usages:
        if user_id is not None and usage["user_id"] != user_id:
            continue
        amount = usage["amount"]
        model_weighted_tokens[usage["model"]] += amount * _token_weight(
            baseline_year, baseline_month, usage["_year"], usage["_month"])
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
            "weighted_total_tokens": model_weighted_tokens.get(model, 0.0),
            "requests": stats["requests"],
            "theoretical_cost": round(model_cost.get(model, 0), 6),
            "cost": round(model_cost.get(model, 0), 6),
        }

    if user_id is None:
        actual = sum(store.user_actual_cost.values())
    else:
        actual = store.user_actual_cost.get(user_id, 0.0)

    total_tokens = total_output + total_input_hit + total_input_miss
    weighted_total_tokens = sum(model_weighted_tokens.values())
    return jsonify({
        "total_output_tokens": total_output,
        "total_input_cache_hit_tokens": total_input_hit,
        "total_input_cache_miss_tokens": total_input_miss,
        "total_input_tokens": total_input_hit + total_input_miss,
        "total_tokens": total_tokens,
        "weighted_total_tokens": weighted_total_tokens,
        "total_requests": total_requests,
        "actual_cost": round(actual, 6),
        "theoretical_total_cost": round(theoretical_total, 6),
        "model_breakdown": model_breakdown,
        "users": store.users,
        "models": store.models,
        "available_months": store.available_months,
    })
