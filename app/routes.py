"""Flask Blueprint: all page routes and API endpoints."""

from collections import defaultdict

from flask import Blueprint, current_app, jsonify, render_template, request

from app.cost_allocator import (compute_proportional_cost,
                                   compute_proportional_cost_by_model)
from app.data_loader import safe_float, safe_int

bp = Blueprint("dashboard", __name__)


def _store():
    """Shortcut to the DataStore singleton stored in Flask app config."""
    return current_app.config["DATA_STORE"]


# ── Page Routes ────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


# ── API Routes ─────────────────────────────────────────────────────────────

@bp.route("/api/refresh")
def api_refresh():
    """Re-scan the data directory for new/modified CSV files."""
    _store().load()
    return jsonify({
        "status": "ok",
        "months": len(_store().available_months),
        "cost_records": len(_store().cost_records),
        "amount_records": len(_store().amount_records),
    })


@bp.route("/api/api_key_names")
def api_api_key_names():
    """Return sorted list of unique api_key_name values."""
    return jsonify(_store().api_key_names)


@bp.route("/api/summary")
def api_summary():
    """Return total token usage statistics across ALL months.

    Query params: api_key_name (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None

    # Token stats from amount records
    total_output = 0
    total_input_hit = 0
    total_input_miss = 0
    total_requests = 0
    model_tokens = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
    })

    for r in _store().amount_records:
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        model = r.get("model", "unknown")

        if rtype == "output_tokens":
            total_output += amount
            model_tokens[model]["output"] += amount
        elif rtype == "input_cache_hit_tokens":
            total_input_hit += amount
            model_tokens[model]["input_hit"] += amount
        elif rtype == "input_cache_miss_tokens":
            total_input_miss += amount
            model_tokens[model]["input_miss"] += amount
        elif rtype == "request_count":
            total_requests += amount
            model_tokens[model]["requests"] += amount

    total_tokens = total_output + total_input_hit + total_input_miss

    # Cost stats
    if api_key_name:
        total_cost = compute_proportional_cost(
            _store().amount_records, _store().cost_records, api_key_name
        )
        model_cost = compute_proportional_cost_by_model(
            _store().amount_records, _store().cost_records, api_key_name
        )
    else:
        total_cost = 0.0
        model_cost = defaultdict(float)
        for r in _store().cost_records:
            cost = safe_float(r.get("cost", 0))
            total_cost += cost
            model = r.get("model", "unknown")
            model_cost[model] += cost
        total_cost = round(total_cost, 4)

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
        "model_breakdown": model_breakdown,
        "available_months": _store().available_months,
        "api_key_names": _store().api_key_names,
    })


@bp.route("/api/monthly")
def api_monthly():
    """Return per-month aggregated token and cost statistics.

    Query params: api_key_name (optional), model (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None
    model_filter = request.args.get("model", "").strip() or None

    # Aggregate amount records by month + model
    monthly_amount = defaultdict(lambda: {
        "output_tokens": 0, "input_cache_hit": 0,
        "input_cache_miss": 0, "requests": 0,
        "by_model": defaultdict(lambda: {"output": 0, "input_hit": 0, "input_miss": 0, "requests": 0}),
    })
    for r in _store().amount_records:
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        if model_filter and r.get("model", "").strip() != model_filter:
            continue
        key = (r["_source_year"], r["_source_month"])
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        model = r.get("model", "unknown")
        if rtype == "output_tokens":
            monthly_amount[key]["output_tokens"] += amount
            monthly_amount[key]["by_model"][model]["output"] += amount
        elif rtype == "input_cache_hit_tokens":
            monthly_amount[key]["input_cache_hit"] += amount
            monthly_amount[key]["by_model"][model]["input_hit"] += amount
        elif rtype == "input_cache_miss_tokens":
            monthly_amount[key]["input_cache_miss"] += amount
            monthly_amount[key]["by_model"][model]["input_miss"] += amount
        elif rtype == "request_count":
            monthly_amount[key]["requests"] += amount
            monthly_amount[key]["by_model"][model]["requests"] += amount

    # Aggregate cost records by month + model
    if api_key_name:
        # Proportional cost allocation — compute shares from ALL amount records
        all_amounts = _store().amount_records
        group_tokens = defaultdict(lambda: defaultdict(int))
        for r in all_amounts:
            uid = r.get("user_id", "")
            date = r.get("utc_date", "")
            rmodel = r.get("model", "unknown")
            kn = r.get("api_key_name", "")
            rtype = r.get("type", "")
            amount = safe_int(r.get("amount", 0))
            if rtype in ("output_tokens", "input_cache_hit_tokens", "input_cache_miss_tokens"):
                group_tokens[(uid, date, rmodel)][kn] += amount

        share = {}
        for gk, kt in group_tokens.items():
            total = sum(kt.values())
            selected = kt.get(api_key_name, 0)
            share[gk] = selected / total if total > 0 else 0.0

        monthly_cost = defaultdict(float)
        monthly_cost_by_model = defaultdict(lambda: defaultdict(float))
        for r in _store().cost_records:
            if model_filter and r.get("model", "").strip() != model_filter:
                continue
            key = (r["_source_year"], r["_source_month"])
            uid = r.get("user_id", "")
            date = r.get("utc_date", "")
            rmodel = r.get("model", "unknown")
            cost = safe_float(r.get("cost", 0))
            fraction = share.get((uid, date, rmodel), 0.0)
            monthly_cost[key] += cost * fraction
            monthly_cost_by_model[key][rmodel] += cost * fraction
    else:
        monthly_cost = defaultdict(float)
        monthly_cost_by_model = defaultdict(lambda: defaultdict(float))
        for r in _store().cost_records:
            if model_filter and r.get("model", "").strip() != model_filter:
                continue
            key = (r["_source_year"], r["_source_month"])
            cost = safe_float(r.get("cost", 0))
            rmodel = r.get("model", "unknown")
            monthly_cost[key] += cost
            monthly_cost_by_model[key][rmodel] += cost

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

    Query params: year (int), month (int), api_key_name (optional), model (optional)
    """
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    api_key_name = request.args.get("api_key_name", "").strip() or None
    model_filter = request.args.get("model", "").strip() or None
    if not year or not month:
        return jsonify({"error": "year and month query params required"}), 400

    # Daily token aggregation
    daily_tokens = defaultdict(lambda: {
        "output_tokens": 0, "input_cache_hit": 0, "input_cache_miss": 0,
        "requests": 0, "by_model": defaultdict(lambda: {
            "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
        })
    })

    for r in _store().amount_records:
        if r["_source_year"] != year or r["_source_month"] != month:
            continue
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        if model_filter and r.get("model", "").strip() != model_filter:
            continue
        day = r.get("utc_date", "")
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        model = r.get("model", "unknown")

        if rtype == "output_tokens":
            daily_tokens[day]["output_tokens"] += amount
            daily_tokens[day]["by_model"][model]["output"] += amount
        elif rtype == "input_cache_hit_tokens":
            daily_tokens[day]["input_cache_hit"] += amount
            daily_tokens[day]["by_model"][model]["input_hit"] += amount
        elif rtype == "input_cache_miss_tokens":
            daily_tokens[day]["input_cache_miss"] += amount
            daily_tokens[day]["by_model"][model]["input_miss"] += amount
        elif rtype == "request_count":
            daily_tokens[day]["requests"] += amount
            daily_tokens[day]["by_model"][model]["requests"] += amount

    # Daily cost aggregation
    if api_key_name:
        # Proportional cost allocation for the selected month
        month_amounts = [r for r in _store().amount_records
                         if r["_source_year"] == year and r["_source_month"] == month]
        month_costs = [r for r in _store().cost_records
                       if r["_source_year"] == year and r["_source_month"] == month]

        group_tokens = defaultdict(lambda: defaultdict(int))
        for r in month_amounts:
            uid = r.get("user_id", "")
            date = r.get("utc_date", "")
            rmodel = r.get("model", "unknown")
            kn = r.get("api_key_name", "")
            rtype = r.get("type", "")
            amount = safe_int(r.get("amount", 0))
            if rtype in ("output_tokens", "input_cache_hit_tokens", "input_cache_miss_tokens"):
                group_tokens[(uid, date, rmodel)][kn] += amount

        share = {}
        for gk, kt in group_tokens.items():
            total = sum(kt.values())
            selected = kt.get(api_key_name, 0)
            share[gk] = selected / total if total > 0 else 0.0

        daily_cost = defaultdict(float)
        daily_cost_by_model = defaultdict(lambda: defaultdict(float))
        for r in month_costs:
            if model_filter and r.get("model", "").strip() != model_filter:
                continue
            day = r.get("utc_date", "")
            uid = r.get("user_id", "")
            date = r.get("utc_date", "")
            rmodel = r.get("model", "unknown")
            cost = safe_float(r.get("cost", 0))
            fraction = share.get((uid, date, rmodel), 0.0)
            daily_cost[day] += cost * fraction
            daily_cost_by_model[day][rmodel] += cost * fraction
    else:
        daily_cost = defaultdict(float)
        daily_cost_by_model = defaultdict(lambda: defaultdict(float))
        for r in _store().cost_records:
            if r["_source_year"] != year or r["_source_month"] != month:
                continue
            if model_filter and r.get("model", "").strip() != model_filter:
                continue
            day = r.get("utc_date", "")
            cost = safe_float(r.get("cost", 0))
            rmodel = r.get("model", "unknown")
            daily_cost[day] += cost
            daily_cost_by_model[day][rmodel] += cost

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

    for r in _store().amount_records:
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        if rtype == "output_tokens":
            total_output += amount
        elif rtype == "input_cache_hit_tokens":
            total_input_hit += amount
        elif rtype == "input_cache_miss_tokens":
            total_input_miss += amount

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

    model_tokens = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
    })

    for r in _store().amount_records:
        if year and r["_source_year"] != year:
            continue
        if month and r["_source_month"] != month:
            continue
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        model = r.get("model", "unknown")
        if rtype == "output_tokens":
            model_tokens[model]["output"] += amount
        elif rtype == "input_cache_hit_tokens":
            model_tokens[model]["input_hit"] += amount
        elif rtype == "input_cache_miss_tokens":
            model_tokens[model]["input_miss"] += amount
        elif rtype == "request_count":
            model_tokens[model]["requests"] += amount

    if api_key_name:
        model_cost = compute_proportional_cost_by_model(
            _store().amount_records, _store().cost_records, api_key_name
        )
    else:
        model_cost = defaultdict(float)
        for r in _store().cost_records:
            if year and r["_source_year"] != year:
                continue
            if month and r["_source_month"] != month:
                continue
            model = r.get("model", "unknown")
            model_cost[model] += safe_float(r.get("cost", 0))

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

    for r in _store().amount_records:
        if year and r["_source_year"] != year:
            continue
        if month and r["_source_month"] != month:
            continue
        if api_key_name and r.get("api_key_name", "").strip() != api_key_name:
            continue
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))
        if rtype == "output_tokens":
            total_output += amount
        elif rtype == "input_cache_hit_tokens":
            total_input_hit += amount
        elif rtype == "input_cache_miss_tokens":
            total_input_miss += amount

    return jsonify([
        {"name": "输出Token", "value": total_output},
        {"name": "输入缓存命中", "value": total_input_hit},
        {"name": "输入缓存未命中", "value": total_input_miss},
    ])
