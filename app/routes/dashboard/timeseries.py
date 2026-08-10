"""Dashboard route group."""

from app.routes.dashboard.common import *  # noqa: F401,F403

@bp.route("/api/daily")
def api_daily():
    """Return daily token/cost breakdown for a given month.

    Query params: year (int), month (int), api_key_name (optional),
                  model (optional), platform (optional)
    """
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    api_key_name = request.args.get("api_key_name", "").strip() or None
    model_filter = request.args.get("model", "").strip() or None
    platform_filter = request.args.get("platform", "").strip() or None
    if not year or not month:
        return jsonify({"error": "year and month query params required"}), 400

    # Daily token aggregation
    daily_tokens = defaultdict(lambda: {
        "output_tokens": 0, "input_cache_hit": 0, "input_cache_miss": 0,
        "requests": 0, "by_model": defaultdict(lambda: {
            "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
        })
    })

    for tu in _store().token_usages:
        if tu["_year"] != year or tu["_month"] != month:
            continue
        if api_key_name and tu["api_key_name"] != api_key_name:
            continue
        if model_filter and tu["model"] != model_filter:
            continue
        if platform_filter and tu["platform"] != platform_filter:
            continue
        day = tu["date"]
        if tu["token_type"] == "output":
            daily_tokens[day]["output_tokens"] += tu["amount"]
            daily_tokens[day]["by_model"][tu["model"]]["output"] += tu["amount"]
        elif tu["token_type"] == "input_cache_hit":
            daily_tokens[day]["input_cache_hit"] += tu["amount"]
            daily_tokens[day]["by_model"][tu["model"]]["input_hit"] += tu["amount"]
        elif tu["token_type"] == "input_cache_miss":
            daily_tokens[day]["input_cache_miss"] += tu["amount"]
            daily_tokens[day]["by_model"][tu["model"]]["input_miss"] += tu["amount"]

    for ru in _store().request_usages:
        if ru["_year"] != year or ru["_month"] != month:
            continue
        if api_key_name and ru["api_key_name"] != api_key_name:
            continue
        if model_filter and ru["model"] != model_filter:
            continue
        if platform_filter and ru["platform"] != platform_filter:
            continue
        day = ru["date"]
        daily_tokens[day]["requests"] += ru["count"]
        daily_tokens[day]["by_model"][ru["model"]]["requests"] += ru["count"]

    # Daily cost aggregation from the canonical V1 usage ledger.  The legacy
    # `cost` field is the api-equivalent amount (theoretical for plan/agent),
    # and `actual_cost` is the metered bill.
    daily_equivalent = defaultdict(float)
    daily_equivalent_by_model = defaultdict(lambda: defaultdict(float))
    daily_actual = defaultdict(float)
    for ce in _store().cost_entries:
        if ce["_year"] != year or ce["_month"] != month:
            continue
        if api_key_name and ce["api_key_name"] != api_key_name:
            continue
        if model_filter and ce["model"] != model_filter:
            continue
        if platform_filter and ce["platform"] != platform_filter:
            continue
        day = ce["date"]
        equiv = float(ce.get("cost", 0) or 0)
        actual = float(ce.get("actual_cost", 0) or 0)
        daily_equivalent[day] += equiv
        daily_equivalent_by_model[day][ce["model"]] += equiv
        daily_actual[day] += actual

    # Build sorted daily result
    sorted_days = sorted(set(daily_tokens.keys()) |
                         set(daily_equivalent.keys()))
    result = []
    for day in sorted_days:
        dt = daily_tokens[day]
        result.append({
            "date": day,
            "output_tokens": dt["output_tokens"],
            "input_cache_hit_tokens": dt["input_cache_hit"],
            "input_cache_miss_tokens": dt["input_cache_miss"],
            "input_tokens": dt["input_cache_hit"] + dt["input_cache_miss"],
            "total_tokens": (dt["output_tokens"] + dt["input_cache_hit"] +
                             dt["input_cache_miss"]),
            "requests": dt["requests"],
            "cost": round(daily_equivalent.get(day, 0), 4),
            "theoretical_cost": round(daily_equivalent.get(day, 0), 4),
            "actual_cost": round(daily_actual.get(day, 0), 4),
            "by_model": {
                m: {
                    "output_tokens": v["output"],
                    "input_cache_hit_tokens": v["input_hit"],
                    "input_cache_miss_tokens": v["input_miss"],
                    "total_tokens": v["output"] + v["input_hit"] + v["input_miss"],
                    "requests": v["requests"],
                    "cost": round(
                        daily_equivalent_by_model.get(day, {}).get(m, 0), 4),
                    "theoretical_cost": round(
                        daily_equivalent_by_model.get(day, {}).get(m, 0), 4),
                }
                for m, v in sorted(dt["by_model"].items())
            },
        })

    return jsonify({
        "year": year,
        "month": month,
        "days": result,
    })


@bp.route("/api/token_types")
def api_token_types():
    """Return aggregated token type breakdown (for pie chart).

    Query params: api_key_name (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None
    total_output = 0
    total_input_hit = 0
    total_input_miss = 0

    for tu in _store().token_usages:
        if api_key_name and tu["api_key_name"] != api_key_name:
            continue
        if tu["token_type"] == "output":
            total_output += tu["amount"]
        elif tu["token_type"] == "input_cache_hit":
            total_input_hit += tu["amount"]
        elif tu["token_type"] == "input_cache_miss":
            total_input_miss += tu["amount"]

    return jsonify([
        {"name": "输出Token", "value": total_output},
        {"name": "输入缓存命中", "value": total_input_hit},
        {"name": "输入缓存未命中", "value": total_input_miss},
    ])
