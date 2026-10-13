"""Historical-attribution reads for actual costs.

Every figure is attributed through ``account_identities``: request rows use
``COALESCE(r.account_identity_id, r.account_id)`` and frozen period charges
use their own identity snapshot (falling back to the contract's account for
legacy rows).  Live routing rows are never consulted, so deleting an account
or software — or renaming one — cannot change a historical report: deletion
only stops future imports/routing, and ``name`` is a display attribute.

Rows with no attributable identity are excluded.  Frozen rows stay frozen
facts: only the ``finalized`` flag and the period window gate the totals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _window(now: datetime | None, days: int) -> tuple[str, str]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def attributed_request_sql(alias: str = "r", *, start_param: str = ":start",
                           end_param: str = ":end") -> str:
    """Return the historical-identity request fragment.

    The fragment joins ``account_identities`` as ``ai`` and ends with the
    window predicate, so callers may project ``ai.id``/``ai.name``/
    ``ai.account_kind`` and append further ``AND`` predicates.  It carries no
    live credential/client-key/agent-software existence test: a report must
    attribute history even after the live configuration row is deleted.
    """
    return f"""
        JOIN account_identities ai
          ON ai.id=COALESCE({alias}.account_identity_id,{alias}.account_id)
        WHERE ai.account_kind IN ('proxy','agent')
          AND {alias}.requested_at>={start_param}
          AND {alias}.requested_at<={end_param}
    """


def _attributed_request_filter(alias: str = "r") -> str:
    """Attributed request fragment with stable named parameters."""
    return attributed_request_sql(alias, start_param=":start", end_param=":end")


def _proxy_charge_sql() -> str:
    """Frozen per-key proxy charges attributed to the historical identity.

    ``c.credential_uuid IS NOT NULL`` keeps the exact row set the previous
    ``billing_scope='credential'`` gate selected (per-account charges carry a
    NULL uuid) without reading the possibly-deleted contract.  No live join
    remains, so a finalized charge stays reportable after its live graph is
    removed.
    """
    return """
        FROM billing_period_charges c
        JOIN account_identities ai
          ON ai.id=COALESCE(
               c.account_identity_id,
               (SELECT bc.account_id FROM billing_contracts bc
                WHERE bc.id=c.contract_id))
        WHERE ai.account_kind='proxy'
          AND c.credential_uuid IS NOT NULL
          AND c.finalized_at IS NOT NULL
          AND c.normalized_recurring_cost IS NOT NULL
          AND c.period_start>=:start AND c.period_start<=:end
    """


def _agent_allocation_sql() -> str:
    """Legacy V2.1 allocations attributed to the historical software id."""
    return """
        FROM agent_subscription_charge_allocations allocation
        JOIN agent_subscription_period_charges charge
          ON charge.id=allocation.period_charge_id
        JOIN account_identities ai ON ai.id=allocation.software_id
        WHERE allocation.is_finalized=1
          AND allocation.normalized_recurring_cost IS NOT NULL
          AND charge.is_finalized=1
          AND charge.software_id IS NULL
          AND ai.account_kind='agent'
          AND charge.period_start>=:start AND charge.period_start<=:end
    """


def _agent_charge_sql() -> str:
    """Directly-owned Agent charges attributed by the frozen software id.

    ``software_id`` is written on the charge at materialization time and
    shares the identity id space, so no live subscription/instance/software
    join (and no date-sensitive binding join) is needed.
    """
    return """
        FROM agent_subscription_period_charges charge
        JOIN account_identities ai ON ai.id=charge.software_id
        WHERE charge.software_id IS NOT NULL
          AND ai.account_kind='agent'
          AND charge.is_finalized=1
          AND charge.normalized_recurring_cost IS NOT NULL
          AND charge.period_start>=:start AND charge.period_start<=:end
    """


def actual_cost(conn, *, now: datetime | None = None, days: int = 30) -> dict:
    """Return attributed actual cost for a UTC rolling window.

    Metered cost comes from request rows via their identity; recurring cost
    from frozen period charges via their identity snapshots.  Deleting or
    renaming the live configuration never changes a past window.
    """
    start, end = _window(now, days)
    params = {"start": start, "end": end}
    metered = conn.execute(
        "SELECT COALESCE(SUM(r.billed_usage_cost),0) FROM request_log r "
        + _attributed_request_filter("r"), params
    ).fetchone()[0]
    proxy = conn.execute(
        "SELECT COALESCE(SUM(c.normalized_recurring_cost),0) "
        + _proxy_charge_sql(), params,
    ).fetchone()[0]
    agent = conn.execute(
        "SELECT COALESCE(SUM(charge.normalized_recurring_cost),0) "
        + _agent_charge_sql(), params,
    ).fetchone()[0]
    legacy_agent = conn.execute(
        "SELECT COALESCE(SUM(allocation.normalized_recurring_cost),0) "
        + _agent_allocation_sql(), params,
    ).fetchone()[0]
    recurring = (float(proxy or 0) + float(agent or 0)
                 + float(legacy_agent or 0))
    return {
        "metered_cost": float(metered or 0),
        "recurring_cost": recurring,
        "total_cost": float(metered or 0) + recurring,
    }


def recurring_by_period_start(conn, *, now: datetime | None = None,
                              days: int = 30) -> dict[str, float]:
    start, end = _window(now, days)
    params = {"start": start, "end": end}
    result: dict[str, float] = {}
    for row in conn.execute(
        "SELECT date(c.period_start) period_start,"
        "COALESCE(SUM(c.normalized_recurring_cost),0) cost "
        + _proxy_charge_sql() + "GROUP BY date(c.period_start)", params
    ):
        period_start, cost = row[0], row[1]
        result[period_start] = result.get(period_start, 0.0) + float(cost or 0)
    for row in conn.execute(
        "SELECT date(charge.period_start) period_start,"
        "COALESCE(SUM(charge.normalized_recurring_cost),0) cost "
        + _agent_charge_sql() + "GROUP BY date(charge.period_start)", params,
    ):
        period_start, cost = row[0], row[1]
        result[period_start] = result.get(period_start, 0.0) + float(cost or 0)
    for row in conn.execute(
        "SELECT date(charge.period_start) period_start,"
        "COALESCE(SUM(allocation.normalized_recurring_cost),0) cost "
        + _agent_allocation_sql() + "GROUP BY date(charge.period_start)", params,
    ):
        period_start, cost = row[0], row[1]
        result[period_start] = result.get(period_start, 0.0) + float(cost or 0)
    return result


def actual_cost_by_day(conn, *, now: datetime | None = None,
                       days: int = 30) -> dict[str, dict[str, float]]:
    """Return attributed metered/recurring/total cost by UTC day."""
    start, end = _window(now, days)
    params = {"start": start, "end": end}
    result: dict[str, dict[str, float]] = {}

    for row in conn.execute(
        "SELECT date(r.requested_at) day,"
        "COALESCE(SUM(r.billed_usage_cost),0) cost "
        "FROM request_log r " + _attributed_request_filter("r") +
        " GROUP BY date(r.requested_at)", params):
        result[str(row[0])] = {
            "metered_cost": float(row[1] or 0),
            "recurring_cost": 0.0,
        }

    for row in conn.execute(
        "SELECT date(c.period_start) day,"
        "COALESCE(SUM(c.normalized_recurring_cost),0) cost "
        + _proxy_charge_sql() + "GROUP BY date(c.period_start)", params):
        item = result.setdefault(str(row[0]), {
            "metered_cost": 0.0, "recurring_cost": 0.0,
        })
        item["recurring_cost"] += float(row[1] or 0)

    for row in conn.execute(
        "SELECT date(charge.period_start) day,"
        "COALESCE(SUM(charge.normalized_recurring_cost),0) cost "
        + _agent_charge_sql() + "GROUP BY date(charge.period_start)", params):
        item = result.setdefault(str(row[0]), {
            "metered_cost": 0.0, "recurring_cost": 0.0,
        })
        item["recurring_cost"] += float(row[1] or 0)

    for row in conn.execute(
        "SELECT date(charge.period_start) day,"
        "COALESCE(SUM(allocation.normalized_recurring_cost),0) cost "
        + _agent_allocation_sql() + "GROUP BY date(charge.period_start)", params):
        item = result.setdefault(str(row[0]), {
            "metered_cost": 0.0, "recurring_cost": 0.0,
        })
        item["recurring_cost"] += float(row[1] or 0)

    for item in result.values():
        item["cost"] = item["metered_cost"] + item["recurring_cost"]
    return result


__all__ = ["_agent_allocation_sql", "_agent_charge_sql", "_proxy_charge_sql",
           "actual_cost", "actual_cost_by_day", "attributed_request_sql",
           "recurring_by_period_start"]
