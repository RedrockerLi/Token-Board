"""Flask Blueprint: all page routes and API endpoints."""

from collections import defaultdict

from flask import Blueprint, current_app, jsonify, render_template, request

from app.services.cost_allocator import (compute_proportional_cost,
                                         compute_proportional_cost_by_model,
                                         compute_proportional_cost_by_month,
                                         compute_proportional_cost_by_day)


bp = Blueprint("dashboard", __name__)


def _store():
    """Shortcut to the DataStore singleton stored in Flask app config."""
    return current_app.config["DATA_STORE"]


# ── Page Routes ────────────────────────────────────────────────────────

@bp.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


# ── API Routes ─────────────────────────────────────────────────────────

@bp.route("/api/refresh")
def api_refresh():
    """Rebuild the in-memory store from the dashboard archive."""
    _store().load()
    return jsonify({
        "status": "ok",
        "months": len(_store().available_months),
        "token_records": len(_store().token_usages),
        "request_records": len(_store().request_usages),
        "cost_records": len(_store().cost_entries),
    })


@bp.route("/api/api_key_names")
def api_api_key_names():
    """Return sorted list of unique api_key_name values."""
    return jsonify(_store().api_key_names)


@bp.route("/api/models")
def api_models():
    """Return models grouped by platform.

    Returns:
        dict: {model_name: {"platform": platform_name, ...}}
    """
    model_map: dict[str, dict] = {}
    for tu in _store().token_usages:
        if tu["model"] not in model_map:
            model_map[tu["model"]] = {"platform": tu["platform"]}
    for ru in _store().request_usages:
        if ru["model"] not in model_map:
            model_map[ru["model"]] = {"platform": ru["platform"]}
    for ce in _store().cost_entries:
        if ce["model"] not in model_map:
            model_map[ce["model"]] = {"platform": ce["platform"]}
    return jsonify(model_map)


@bp.route("/api/summary")
def api_summary():
    """Return total token usage statistics across ALL months.

    Query params: api_key_name (optional), platform (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None
    platform_filter = request.args.get("platform", "").strip() or None

    # Token stats from token_usages
    total_output = 0
    total_input_hit = 0
    total_input_miss = 0
    model_tokens: dict[str, dict] = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
    })

    for tu in _store().token_usages:
        if api_key_name and tu["api_key_name"] != api_key_name:
            continue
        if platform_filter and tu["platform"] != platform_filter:
            continue
        if tu["token_type"] == "output":
            total_output += tu["amount"]
            model_tokens[tu["model"]]["output"] += tu["amount"]
        elif tu["token_type"] == "input_cache_hit":
            total_input_hit += tu["amount"]
            model_tokens[tu["model"]]["input_hit"] += tu["amount"]
        elif tu["token_type"] == "input_cache_miss":
            total_input_miss += tu["amount"]
            model_tokens[tu["model"]]["input_miss"] += tu["amount"]

    total_requests = 0
    for ru in _store().request_usages:
        if api_key_name and ru["api_key_name"] != api_key_name:
            continue
        if platform_filter and ru["platform"] != platform_filter:
            continue
        total_requests += ru["count"]
        model_tokens[ru["model"]]["requests"] += ru["count"]

    total_tokens = total_output + total_input_hit + total_input_miss

    # Cost stats.
    # A plan account's cost_entry now carries its API-equivalent (theoretical)
    # bill, so it must be EXCLUDED from the *real* total_cost (which is
    # api-account usage + plan subscriptions, added below) — otherwise the plan
    # virtual bill would be double-counted with its subscription. The
    # plan accounts' api-equivalent shows up only in the per-model usage cards
    # (api_daily / api_monthly sum ALL cost_entry rows).
    plan_account_names = {
        ps.get("account_name") for ps in _store().plan_summary if ps.get("account_name")
    }

    def _non_plan(ces):
        return [ce for ce in ces if ce["cost_group_key"] not in plan_account_names]

    if api_key_name:
        total_cost = compute_proportional_cost(
            _store().token_usages, _non_plan(_store().cost_entries), api_key_name
        )
        model_cost = compute_proportional_cost_by_model(
            _store().token_usages, _non_plan(_store().cost_entries), api_key_name
        )
    else:
        total_cost = 0.0
        model_cost: dict[str, float] = defaultdict(float)
        for ce in _non_plan(_store().cost_entries):
            if platform_filter and ce["platform"] != platform_filter:
                continue
            total_cost += ce["cost"]
            model_cost[ce["model"]] += ce["cost"]
        total_cost = round(total_cost, 4)

    # Plan economics (proxy-exported data). When a specific user is selected,
    # only that user's plan rows count (so a plan account shows its own
    # subscription + virtual cost; api / CSV users get 0). In the overview all
    # plan rows count. Frontend adds these on top of total_cost:
    #   subscription_cost = real: plan monthly prices, one per used month
    #   virtual_cost      = theoretical: api-billed amount the plan covered
    if api_key_name:
        plan_rows = [
            ps for ps in _store().plan_summary
            if ps.get("account_name") == api_key_name
        ]
    else:
        plan_rows = _store().plan_summary
    plan_subscription_cost = sum(ps.get("subscription_cost", 0) for ps in plan_rows)
    plan_virtual_cost = sum(ps.get("virtual_cost", 0) for ps in plan_rows)
    plan_subscription_cost = round(plan_subscription_cost, 4)
    plan_virtual_cost = round(plan_virtual_cost, 4)

    # Build model breakdown
    all_models = set(model_tokens.keys()) | set(model_cost.keys())
    model_breakdown = {}
    for m in sorted(all_models):
        mt = model_tokens[m]
        model_breakdown[m] = {
            "output_tokens": mt["output"],
            "input_cache_hit_tokens": mt["input_hit"],
            "input_cache_miss_tokens": mt["input_miss"],
            "total_tokens": mt["output"] + mt["input_hit"] + mt["input_miss"],
            "requests": mt["requests"],
            "cost": round(model_cost.get(m, 0), 4),
        }

    return jsonify({
        "total_output_tokens": total_output,
        "total_input_cache_hit_tokens": total_input_hit,
        "total_input_cache_miss_tokens": total_input_miss,
        "total_input_tokens": total_input_hit + total_input_miss,
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "total_cost": total_cost,
        "plan_subscription_cost": plan_subscription_cost,
        "plan_virtual_cost": plan_virtual_cost,
        "model_breakdown": model_breakdown,
        "available_months": _store().available_months,
        "api_key_names": _store().api_key_names,
        "platforms": _store().platforms,
        "models": _store().models,
    })


@bp.route("/api/monthly")
def api_monthly():
    """Return per-month aggregated token and cost statistics.

    Query params: api_key_name (optional), model (optional), platform (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None
    model_filter = request.args.get("model", "").strip() or None
    platform_filter = request.args.get("platform", "").strip() or None

    # Aggregate token_usages by month + model
    monthly_amount = defaultdict(lambda: {
        "output_tokens": 0, "input_cache_hit": 0,
        "input_cache_miss": 0, "requests": 0,
        "by_model": defaultdict(lambda: {"output": 0, "input_hit": 0,
                                          "input_miss": 0, "requests": 0}),
    })
    for tu in _store().token_usages:
        if api_key_name and tu["api_key_name"] != api_key_name:
            continue
        if model_filter and tu["model"] != model_filter:
            continue
        if platform_filter and tu["platform"] != platform_filter:
            continue
        key = (tu["_year"], tu["_month"])
        if tu["token_type"] == "output":
            monthly_amount[key]["output_tokens"] += tu["amount"]
            monthly_amount[key]["by_model"][tu["model"]]["output"] += tu["amount"]
        elif tu["token_type"] == "input_cache_hit":
            monthly_amount[key]["input_cache_hit"] += tu["amount"]
            monthly_amount[key]["by_model"][tu["model"]]["input_hit"] += tu["amount"]
        elif tu["token_type"] == "input_cache_miss":
            monthly_amount[key]["input_cache_miss"] += tu["amount"]
            monthly_amount[key]["by_model"][tu["model"]]["input_miss"] += tu["amount"]

    for ru in _store().request_usages:
        if api_key_name and ru["api_key_name"] != api_key_name:
            continue
        if model_filter and ru["model"] != model_filter:
            continue
        if platform_filter and ru["platform"] != platform_filter:
            continue
        key = (ru["_year"], ru["_month"])
        monthly_amount[key]["requests"] += ru["count"]
        monthly_amount[key]["by_model"][ru["model"]]["requests"] += ru["count"]

    # Aggregate cost entries by month + model
    if api_key_name:
        # Proportional cost allocation — shares from ALL token_usages
        monthly_cost, monthly_cost_by_model = compute_proportional_cost_by_month(
            _store().token_usages,
            [ce for ce in _store().cost_entries
             if (not model_filter or ce["model"] == model_filter)
             and (not platform_filter or ce["platform"] == platform_filter)],
            api_key_name,
        )
    else:
        monthly_cost = defaultdict(float)
        monthly_cost_by_model = defaultdict(lambda: defaultdict(float))
        for ce in _store().cost_entries:
            if model_filter and ce["model"] != model_filter:
                continue
            if platform_filter and ce["platform"] != platform_filter:
                continue
            key = (ce["_year"], ce["_month"])
            monthly_cost[key] += ce["cost"]
            monthly_cost_by_model[key][ce["model"]] += ce["cost"]

    result = []
    for m in _store().available_months:
        key = (m["year"], m["month"])
        am = monthly_amount.get(key, {})
        by_model = {}
        for mdl, v in sorted(am.get("by_model", {}).items()):
            by_model[mdl] = {
                "output_tokens": v["output"],
                "input_cache_hit_tokens": v["input_hit"],
                "input_cache_miss_tokens": v["input_miss"],
                "total_tokens": v["output"] + v["input_hit"] + v["input_miss"],
                "requests": v["requests"],
                "cost": round(monthly_cost_by_model.get(key, {}).get(mdl, 0), 4),
            }
        result.append({
            "year": m["year"],
            "month": m["month"],
            "label": m["label"],
            "output_tokens": am.get("output_tokens", 0),
            "input_cache_hit_tokens": am.get("input_cache_hit", 0),
            "input_cache_miss_tokens": am.get("input_cache_miss", 0),
            "input_tokens": am.get("input_cache_hit", 0) + am.get("input_cache_miss", 0),
            "total_tokens": (am.get("output_tokens", 0) +
                             am.get("input_cache_hit", 0) +
                             am.get("input_cache_miss", 0)),
            "requests": am.get("requests", 0),
            "cost": round(monthly_cost.get(key, 0), 4),
            "by_model": by_model,
        })

    return jsonify(result)


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

    # Daily cost aggregation
    if api_key_name:
        # Proportional cost allocation for the selected month — shares from
        # that month's tokens only
        month_tokens = [tu for tu in _store().token_usages
                        if tu["_year"] == year and tu["_month"] == month]
        month_costs = [ce for ce in _store().cost_entries
                       if ce["_year"] == year and ce["_month"] == month]
        daily_cost, daily_cost_by_model = compute_proportional_cost_by_day(
            month_tokens,
            [ce for ce in month_costs
             if (not model_filter or ce["model"] == model_filter)
             and (not platform_filter or ce["platform"] == platform_filter)],
            api_key_name,
        )
    else:
        daily_cost = defaultdict(float)
        daily_cost_by_model = defaultdict(lambda: defaultdict(float))
        for ce in _store().cost_entries:
            if ce["_year"] != year or ce["_month"] != month:
                continue
            if model_filter and ce["model"] != model_filter:
                continue
            if platform_filter and ce["platform"] != platform_filter:
                continue
            day = ce["date"]
            daily_cost[day] += ce["cost"]
            daily_cost_by_model[day][ce["model"]] += ce["cost"]

    # Build sorted daily result
    sorted_days = sorted(set(daily_tokens.keys()) | set(daily_cost.keys()))
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
            "cost": round(daily_cost.get(day, 0), 4),
            "by_model": {
                m: {
                    "output_tokens": v["output"],
                    "input_cache_hit_tokens": v["input_hit"],
                    "input_cache_miss_tokens": v["input_miss"],
                    "total_tokens": v["output"] + v["input_hit"] + v["input_miss"],
                    "requests": v["requests"],
                    "cost": round(daily_cost_by_model.get(day, {}).get(m, 0), 4),
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

    if api_key_name:
        model_cost = compute_proportional_cost_by_model(
            _store().token_usages, _store().cost_entries, api_key_name
        )
    else:
        model_cost = defaultdict(float)
        for ce in _store().cost_entries:
            if year and ce["_year"] != year:
                continue
            if month and ce["_month"] != month:
                continue
            model_cost[ce["model"]] += ce["cost"]

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
