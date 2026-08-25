"""Dashboard route group."""

from collections import defaultdict

from app.routes.dashboard.common import bp, current_app, jsonify, request, _store

@bp.route("/api/model_breakdown")
def api_model_breakdown():
    """Return model breakdown, optionally filtered by year/month and api_key_name.

    Query params: year (int), month (int), api_key_name (optional)
    """
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    api_key_name = request.args.get("api_key_name", "").strip() or None

    model_tokens: dict[str, dict] = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
    })

    for tu in _store().token_usages:
        if year and tu["_year"] != year:
            continue
        if month and tu["_month"] != month:
            continue
        if api_key_name and tu["api_key_name"] != api_key_name:
            continue
        if tu["token_type"] == "output":
            model_tokens[tu["model"]]["output"] += tu["amount"]
        elif tu["token_type"] == "input_cache_hit":
            model_tokens[tu["model"]]["input_hit"] += tu["amount"]
        elif tu["token_type"] == "input_cache_miss":
            model_tokens[tu["model"]]["input_miss"] += tu["amount"]

    for ru in _store().request_usages:
        if year and ru["_year"] != year:
            continue
        if month and ru["_month"] != month:
            continue
        if api_key_name and ru["api_key_name"] != api_key_name:
            continue
        model_tokens[ru["model"]]["requests"] += ru["count"]

    model_cost = defaultdict(float)
    model_theoretical = defaultdict(float)
    for ce in _store().cost_entries:
        if api_key_name and ce["api_key_name"] != api_key_name:
            continue
        if year and ce["_year"] != year:
            continue
        if month and ce["_month"] != month:
            continue
        model_cost[ce["model"]] += float(ce.get("cost", 0) or 0)
        model_theoretical[ce["model"]] += float(
            ce.get("theoretical_cost", 0) or 0)

    result = {}
    all_models = set(model_tokens.keys()) | set(model_cost.keys())
    for m in sorted(all_models):
        mt = model_tokens[m]
        result[m] = {
            "output_tokens": mt["output"],
            "input_cache_hit_tokens": mt["input_hit"],
            "input_cache_miss_tokens": mt["input_miss"],
            "total_tokens": mt["output"] + mt["input_hit"] + mt["input_miss"],
            "requests": mt["requests"],
            "cost": round(model_cost.get(m, 0), 4),
            "theoretical_cost": round(model_theoretical.get(m, 0), 4),
        }
    return jsonify(result)


@bp.route("/api/token_types_by_month")
def api_token_types_by_month():
    """Return token type breakdown filtered by year/month and api_key_name.

    Query params: year (int), month (int), api_key_name (optional)
    """
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    api_key_name = request.args.get("api_key_name", "").strip() or None

    total_output = 0
    total_input_hit = 0
    total_input_miss = 0

    for tu in _store().token_usages:
        if year and tu["_year"] != year:
            continue
        if month and tu["_month"] != month:
            continue
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
