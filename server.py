#!/usr/bin/env python3
"""DeepSeek API Usage Visualization Dashboard Server.

Reads cost and amount CSV files from the data/ directory and serves
a web dashboard with token usage statistics and ECharts visualizations.
"""

import csv
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Suppress Flask/Werkzeug startup noise
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)
cli = logging.getLogger("flask.app")
cli.setLevel(logging.ERROR)

app = Flask(__name__)

# ── Data storage ──────────────────────────────────────────────────────────────
# Populated by load_all_data() on startup and on /api/refresh
DATA_STORE = {
    "cost_records": [],       # list of dicts from cost-*.csv
    "amount_records": [],     # list of dicts from amount-*.csv
    "available_months": [],   # sorted list of {"year": Y, "month": M, "label": "YYYY-MM"}
    "api_key_names": [],      # sorted list of unique api_key_name strings
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


# ── Data Loading ──────────────────────────────────────────────────────────────

def parse_filename(filename: str) -> tuple[str, int, int] | None:
    """Extract (type, year, month) from filenames like 'cost-2026-5.csv'."""
    m = re.match(r"(cost|amount)-(\d{4})-(\d{1,2})\.csv$", filename)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    return None


def load_all_data():
    """Scan data/ recursively, parse all CSVs, rebuild DATA_STORE."""
    cost_records = []
    amount_records = []
    months_set = set()

    if not DATA_DIR.exists():
        print(f"[WARN] Data directory not found: {DATA_DIR}")
        DATA_STORE["cost_records"] = []
        DATA_STORE["amount_records"] = []
        DATA_STORE["available_months"] = []
        DATA_STORE["api_key_names"] = []
        return

    for root, _dirs, files in os.walk(DATA_DIR):
        for fname in files:
            if not fname.endswith(".csv"):
                continue
            parsed = parse_filename(fname)
            if parsed is None:
                continue
            csv_type, year, month = parsed
            filepath = os.path.join(root, fname)
            months_set.add((year, month))

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Strip whitespace from keys and values
                        cleaned = {k.strip(): v.strip() for k, v in row.items()}
                        cleaned["_source_year"] = year
                        cleaned["_source_month"] = month
                        cleaned["_source_file"] = fname
                        if csv_type == "cost":
                            cost_records.append(cleaned)
                        else:
                            amount_records.append(cleaned)
            except Exception as e:
                print(f"[ERROR] Failed to read {filepath}: {e}")

    # Collect unique api_key_names from amount records
    api_key_names_set = set()
    for r in amount_records:
        name = r.get("api_key_name", "").strip()
        if name:
            api_key_names_set.add(name)

    # Sort months
    sorted_months = sorted(months_set, key=lambda x: (x[0], x[1]))
    available_months = [
        {"year": y, "month": m, "label": f"{y}-{m:02d}"}
        for y, m in sorted_months
    ]

    DATA_STORE["cost_records"] = cost_records
    DATA_STORE["amount_records"] = amount_records
    DATA_STORE["available_months"] = available_months
    DATA_STORE["api_key_names"] = sorted(api_key_names_set)


# ── Helper: safe int/float ────────────────────────────────────────────────────

def safe_int(val, default=0):
    """Convert to int, treating empty string as default."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val, default=0.0):
    """Convert to float, treating empty string as default."""
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ── Helper: proportional cost allocation ────────────────────────────────────────

def compute_proportional_cost(amount_records, cost_records, api_key_name):
    """Allocate costs to a specific api_key_name proportionally by token usage.

    For each (user_id, utc_date, model) group, the cost is split across
    api_key_names based on their share of total tokens in that group.
    """
    # ── Step 1: compute total tokens per (user_id, utc_date, model, api_key_name) ──
    #  group_tokens[(user_id, date, model)][api_key_name] = total_tokens
    group_tokens = defaultdict(lambda: defaultdict(int))

    for r in amount_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        key_name = r.get("api_key_name", "")
        rtype = r.get("type", "")
        amount = safe_int(r.get("amount", 0))

        # Sum all token types for this record
        if rtype in ("output_tokens", "input_cache_hit_tokens", "input_cache_miss_tokens"):
            group_tokens[(uid, date, model)][key_name] += amount

    # ── Step 2: compute share of selected api_key_name per group ──
    #  share[(user_id, date, model)] = fraction (0.0 - 1.0)
    share = {}
    for group_key, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[group_key] = selected_tokens / total if total > 0 else 0.0

    # ── Step 3: allocate cost records proportionally ──
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

    Returns dict: {model_name: cost_float}
    """
    # ── Step 1: compute total tokens per (user_id, utc_date, model, api_key_name) ──
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

    # ── Step 2: compute share of selected api_key_name per group ──
    share = {}
    for group_key, key_tokens in group_tokens.items():
        total = sum(key_tokens.values())
        selected_tokens = key_tokens.get(api_key_name, 0)
        share[group_key] = selected_tokens / total if total > 0 else 0.0

    # ── Step 3: allocate cost records proportionally, by model ──
    model_cost = defaultdict(float)
    for r in cost_records:
        uid = r.get("user_id", "")
        date = r.get("utc_date", "")
        model = r.get("model", "unknown")
        cost = safe_float(r.get("cost", 0))
        fraction = share.get((uid, date, model), 0.0)
        model_cost[model] += cost * fraction

    return {m: round(c, 4) for m, c in model_cost.items()}


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main dashboard page."""
    return render_template("index.html")


@app.route("/api/refresh")
def api_refresh():
    """Re-scan the data directory for new/modified CSV files."""
    load_all_data()
    return jsonify({
        "status": "ok",
        "months": len(DATA_STORE["available_months"]),
        "cost_records": len(DATA_STORE["cost_records"]),
        "amount_records": len(DATA_STORE["amount_records"]),
    })


@app.route("/api/api_key_names")
def api_api_key_names():
    """Return sorted list of unique api_key_name values."""
    return jsonify(DATA_STORE["api_key_names"])


@app.route("/api/summary")
def api_summary():
    """Return total token usage statistics across ALL months.

    Query params: api_key_name (optional) — filter by specific api key name

    Returns:
        total_output_tokens, total_input_cache_hit, total_input_cache_miss,
        total_tokens (sum of above three), total_requests, total_cost,
        model_breakdown: per-model {output, input_hit, input_miss, total, requests, cost}
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None

    # ── Token stats from amount records ──
    total_output = 0
    total_input_hit = 0
    total_input_miss = 0
    total_requests = 0
    model_tokens = defaultdict(lambda: {
        "output": 0, "input_hit": 0, "input_miss": 0, "requests": 0
    })

    for r in DATA_STORE["amount_records"]:
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

    # ── Cost stats ──
    if api_key_name:
        # Proportional cost allocation
        total_cost = compute_proportional_cost(
            DATA_STORE["amount_records"], DATA_STORE["cost_records"], api_key_name
        )
        model_cost = compute_proportional_cost_by_model(
            DATA_STORE["amount_records"], DATA_STORE["cost_records"], api_key_name
        )
    else:
        total_cost = 0.0
        model_cost = defaultdict(float)
        for r in DATA_STORE["cost_records"]:
            cost = safe_float(r.get("cost", 0))
            total_cost += cost
            model = r.get("model", "unknown")
            model_cost[model] += cost
        total_cost = round(total_cost, 4)

    # ── Build model breakdown ──
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
        "available_months": DATA_STORE["available_months"],
        "api_key_names": DATA_STORE["api_key_names"],
    })


@app.route("/api/monthly")
def api_monthly():
    """Return per-month aggregated token and cost statistics with model breakdown.

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
    for r in DATA_STORE["amount_records"]:
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
        all_amounts = DATA_STORE["amount_records"]
        # Build shares per (user_id, date, model)
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
        for r in DATA_STORE["cost_records"]:
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
        for r in DATA_STORE["cost_records"]:
            if model_filter and r.get("model", "").strip() != model_filter:
                continue
            key = (r["_source_year"], r["_source_month"])
            cost = safe_float(r.get("cost", 0))
            rmodel = r.get("model", "unknown")
            monthly_cost[key] += cost
            monthly_cost_by_model[key][rmodel] += cost

    result = []
    for m in DATA_STORE["available_months"]:
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


@app.route("/api/daily")
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

    for r in DATA_STORE["amount_records"]:
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
        month_amounts = [r for r in DATA_STORE["amount_records"]
                         if r["_source_year"] == year and r["_source_month"] == month]
        month_costs = [r for r in DATA_STORE["cost_records"]
                       if r["_source_year"] == year and r["_source_month"] == month]

        # Build shares per (user_id, date, model)
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
        for r in DATA_STORE["cost_records"]:
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


@app.route("/api/token_types")
def api_token_types():
    """Return aggregated token type breakdown (for pie chart).

    Query params: api_key_name (optional)
    """
    api_key_name = request.args.get("api_key_name", "").strip() or None
    total_output = 0
    total_input_hit = 0
    total_input_miss = 0

    for r in DATA_STORE["amount_records"]:
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


@app.route("/api/model_breakdown")
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

    for r in DATA_STORE["amount_records"]:
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
            DATA_STORE["amount_records"], DATA_STORE["cost_records"], api_key_name
        )
    else:
        model_cost = defaultdict(float)
        for r in DATA_STORE["cost_records"]:
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


@app.route("/api/token_types_by_month")
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

    for r in DATA_STORE["amount_records"]:
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


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_all_data()
    app.run(host="0.0.0.0", port=5000, debug=False)
