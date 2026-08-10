"""Dashboard route group."""

from app.routes.dashboard.common import *  # noqa: F401,F403

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

    # Plan/agent rows carry their api-equivalent (theoretical) amount in
    # `cost`; their real economics live in plan_summary.  Exclude those rows
    # from the *real* total (otherwise the plan virtual bill would be
    # double-counted with its subscription), matching the pre-V1 summary.
    plan_account_names = {
        ps.get("account_name") for ps in _store().plan_summary
        if ps.get("account_name")
    }

    def _non_plan(ces):
        return [ce for ce in ces
                if ce["cost_group_key"] not in plan_account_names]

    selected_costs = [
        ce for ce in _store().cost_entries
        if (not api_key_name or ce["api_key_name"] == api_key_name)
        and (not platform_filter or ce["platform"] == platform_filter)
    ]
    non_plan_costs = _non_plan(selected_costs)
    total_cost = round(sum(float(ce.get("cost", 0) or 0)
                           for ce in non_plan_costs), 4)
    theoretical_cost = round(sum(float(ce.get("theoretical_cost", 0) or 0)
                                 for ce in selected_costs), 4)
    billed_cost = round(sum(float(ce.get("actual_cost", 0) or 0)
                            for ce in selected_costs), 4)
    model_cost = defaultdict(float)
    for ce in non_plan_costs:
        model_cost[ce["model"]] += float(ce.get("cost", 0) or 0)

    # Plan economics (proxy-exported data). When a specific user is selected,
    # only that user's plan rows count (so a plan account shows its own
    # subscription + virtual cost; api / CSV users get 0). In the overview all
    # `virtual_cost` is retained as historical/UI provenance.  It is already
    # represented by daily_usage.equivalent_cost and must not be added again.
    if api_key_name:
        plan_rows = [
            ps for ps in _store().plan_summary
            if ps.get("account_name") == api_key_name
        ]
    else:
        plan_rows = _store().plan_summary
    plan_subscription_cost = sum(ps.get("subscription_cost", 0) for ps in plan_rows)
    plan_virtual_cost = sum(ps.get("virtual_cost", 0) for ps in plan_rows)
    billing_incomplete_count = sum(
        int(ps.get("billing_incomplete_count", 0) or 0) for ps in plan_rows)
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
        "theoretical_cost": theoretical_cost,
        # Canonical V1 ledger views. `total_cost`/`theoretical_cost` remain
        # legacy UI fields; these explicit totals are never computed by
        # account_type or aggregate branches.
        "actual_cost": round(billed_cost + plan_subscription_cost, 4),
        "theoretical_total_cost": (
            round(theoretical_cost or 0, 4)
            if theoretical_cost is not None else None
        ),
        "plan_subscription_cost": plan_subscription_cost,
        "plan_virtual_cost": plan_virtual_cost,
        "billing_incomplete_count": billing_incomplete_count,
        "billing_health": "degraded" if billing_incomplete_count else "ok",
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

    # Aggregate cost entries by month + model.  The legacy `cost` field keeps
    # its api-equivalent meaning (theoretical for plan/agent), while the real
    # metered bill is carried by `actual_cost`.
    monthly_equivalent = defaultdict(float)
    monthly_equivalent_by_model = defaultdict(lambda: defaultdict(float))
    monthly_actual = defaultdict(float)
    for ce in _store().cost_entries:
        if api_key_name and ce["api_key_name"] != api_key_name:
            continue
        if model_filter and ce["model"] != model_filter:
            continue
        if platform_filter and ce["platform"] != platform_filter:
            continue
        key = (ce["_year"], ce["_month"])
        equiv = float(ce.get("cost", 0) or 0)
        actual = float(ce.get("actual_cost", 0) or 0)
        monthly_equivalent[key] += equiv
        monthly_equivalent_by_model[key][ce["model"]] += equiv
        monthly_actual[key] += actual

    monthly_recurring = defaultdict(float)
    monthly_incomplete = defaultdict(int)
    for row in _store().plan_summary:
        if api_key_name and row.get("account_name") != api_key_name:
            continue
        try:
            year, month = (int(part) for part in row["month"].split("-")[:2])
        except (AttributeError, TypeError, ValueError):
            continue
        monthly_recurring[(year, month)] += float(
            row.get("subscription_cost", 0) or 0)
        monthly_incomplete[(year, month)] += int(
            row.get("billing_incomplete_count", 0) or 0)

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
                "cost": round(
                    monthly_equivalent_by_model.get(key, {}).get(mdl, 0), 4),
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
            "cost": round(monthly_equivalent.get(key, 0), 4),
            "theoretical_cost": round(monthly_equivalent.get(key, 0), 4),
            "actual_cost": round(monthly_actual.get(key, 0) +
                                 monthly_recurring.get(key, 0), 4),
            "billing_incomplete_count": monthly_incomplete.get(key, 0),
            "theoretical_total_cost": round(monthly_equivalent.get(key, 0), 4),
            "by_model": by_model,
        })

    return jsonify(result)
