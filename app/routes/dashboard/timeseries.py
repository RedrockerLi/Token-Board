"""Token/request time-series endpoints for the homepage."""

from collections import defaultdict

from app.routes.dashboard.common import _store, api_error, bp, jsonify, request


def _selected_user_id():
    raw = request.args.get("user_id", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@bp.route("/api/daily")
def api_daily():
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    user_id = _selected_user_id()
    model_filter = request.args.get("model", "").strip() or None
    if not year or not month:
        return api_error("year and month query params required", 400)

    daily = defaultdict(lambda: {
        "output_tokens": 0, "input_cache_hit": 0, "input_cache_miss": 0,
        "requests": 0, "by_model": defaultdict(lambda: {
            "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0,
        }),
    })
    for usage in _store().token_usages:
        if usage["_year"] != year or usage["_month"] != month:
            continue
        if user_id is not None and usage["user_id"] != user_id:
            continue
        if model_filter and usage["model"] != model_filter:
            continue
        day = daily[usage["date"]]
        model = day["by_model"][usage["model"]]
        if usage["token_type"] == "output":
            day["output_tokens"] += usage["amount"]
            model["output"] += usage["amount"]
        elif usage["token_type"] == "input_cache_hit":
            day["input_cache_hit"] += usage["amount"]
            model["input_hit"] += usage["amount"]
        else:
            day["input_cache_miss"] += usage["amount"]
            model["input_miss"] += usage["amount"]

    for usage in _store().request_usages:
        if usage["_year"] != year or usage["_month"] != month:
            continue
        if user_id is not None and usage["user_id"] != user_id:
            continue
        if model_filter and usage["model"] != model_filter:
            continue
        day = daily[usage["date"]]
        day["requests"] += usage["count"]
        day["by_model"][usage["model"]]["requests"] += usage["count"]

    result = []
    for day_name in sorted(daily):
        day = daily[day_name]
        result.append({
            "date": day_name,
            "output_tokens": day["output_tokens"],
            "input_cache_hit_tokens": day["input_cache_hit"],
            "input_cache_miss_tokens": day["input_cache_miss"],
            "input_tokens": day["input_cache_hit"] + day["input_cache_miss"],
            "total_tokens": day["output_tokens"] + day["input_cache_hit"] + day["input_cache_miss"],
            "requests": day["requests"],
            "by_model": {
                model: {
                    "output_tokens": values["output"],
                    "input_cache_hit_tokens": values["input_hit"],
                    "input_cache_miss_tokens": values["input_miss"],
                    "total_tokens": values["output"] + values["input_hit"] + values["input_miss"],
                    "requests": values["requests"],
                }
                for model, values in sorted(day["by_model"].items())
            },
        })
    return jsonify({"year": year, "month": month, "days": result})


@bp.route("/api/token_types")
def api_token_types():
    user_id = _selected_user_id()
    totals = {"output": 0, "input_cache_hit": 0, "input_cache_miss": 0}
    for usage in _store().token_usages:
        if user_id is not None and usage["user_id"] != user_id:
            continue
        totals[usage["token_type"]] += usage["amount"]
    return jsonify([
        {"name": "输出Token", "value": totals["output"]},
        {"name": "输入缓存命中", "value": totals["input_cache_hit"]},
        {"name": "输入缓存未命中", "value": totals["input_cache_miss"]},
    ])
