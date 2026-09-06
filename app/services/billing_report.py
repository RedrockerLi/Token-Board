"""One read interface for current-state actual costs.

Frozen ledgers remain immutable history, but they are not proof that the
corresponding live billing unit still exists.  Every query in this module
therefore uses the ledger for the amount and live V2 rows for eligibility.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _window(now: datetime | None, days: int) -> tuple[str, str]:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def live_request_sql(alias: str = "r", *, start_param: str = ":start",
                     end_param: str = ":end", now_param: str = ":now") -> str:
    """Return the current-configuration request filter.

    The request log intentionally keeps detached historical rows.  A current
    consumption report must not fall back to ``account_identities`` for those
    rows.  A nullable credential/client-key reference is accepted because
    imported usage and some internal routes do not carry that reference; when
    present, it must still resolve to a live row.
    """
    return f"""
        JOIN accounts live_account ON live_account.id={alias}.account_id
        WHERE live_account.account_kind IN ('proxy','agent')
          AND (
                (live_account.account_kind='proxy'
                 AND ({alias}.credential_uuid IS NULL OR EXISTS(
                    SELECT 1 FROM upstream_credentials live_credential
                    JOIN upstreams live_upstream
                      ON live_upstream.id=live_credential.upstream_id
                    WHERE live_credential.uuid={alias}.credential_uuid
                      AND live_upstream.account_id=live_account.id
                      AND (live_credential.ends_at IS NULL
                           OR live_credential.ends_at>{now_param})
                 )))
                OR
                (live_account.account_kind='agent'
                 AND EXISTS(
                    SELECT 1 FROM agent_software live_software
                    WHERE live_software.id={alias}.agent_software_id
                 ))
              )
          AND ({alias}.client_key_id IS NULL OR EXISTS(
                SELECT 1 FROM client_keys live_client_key
                WHERE live_client_key.id={alias}.client_key_id
              ))
          AND {alias}.requested_at>={start_param}
          AND {alias}.requested_at<={end_param}
    """


def _live_request_filter(alias: str = "r") -> str:
    """Current request filter with stable named parameters."""
    return live_request_sql(alias, start_param=":start", end_param=":end")


def _proxy_charge_sql() -> str:
    return """
        FROM billing_period_charges c
        JOIN billing_contracts bc ON bc.id=c.contract_id
        JOIN upstream_credentials live_credential
          ON live_credential.uuid=c.credential_uuid
        JOIN upstreams live_upstream
          ON live_upstream.id=live_credential.upstream_id
        JOIN accounts live_account
          ON live_account.id=live_upstream.account_id
         AND live_account.account_kind='proxy'
        WHERE c.finalized_at IS NOT NULL
          AND c.normalized_recurring_cost IS NOT NULL
          AND bc.charge_type='recurring'
          AND bc.billing_scope='credential'
          AND bc.account_id=live_account.id
          AND (live_credential.ends_at IS NULL
               OR live_credential.ends_at>:now)
          AND (bc.ends_at IS NULL OR bc.ends_at>:now)
          AND c.period_start>=:start AND c.period_start<=:end
    """


def _agent_allocation_sql() -> str:
    return """
        FROM agent_subscription_charge_allocations allocation
        JOIN agent_subscription_period_charges charge
          ON charge.id=allocation.period_charge_id
        JOIN agent_subscriptions live_subscription
          ON live_subscription.id=charge.subscription_id
        JOIN agent_subscription_instances live_instance
          ON live_instance.id=charge.instance_id
         AND live_instance.subscription_id=live_subscription.id
        JOIN agent_software live_software
          ON live_software.id=allocation.software_id
        JOIN accounts live_agent_account
          ON live_agent_account.id=live_software.id
         AND live_agent_account.account_kind='agent'
        WHERE allocation.finalized_at IS NOT NULL
          AND allocation.normalized_recurring_cost IS NOT NULL
          AND charge.finalized_at IS NOT NULL
          AND (live_subscription.ends_at IS NULL
               OR live_subscription.ends_at>:now)
          AND (live_instance.ends_at IS NULL OR live_instance.ends_at>:now)
          AND EXISTS(
                SELECT 1 FROM agent_subscription_bindings live_binding
                WHERE live_binding.subscription_id=live_subscription.id
                  AND live_binding.software_id=allocation.software_id
                  AND live_binding.valid_from<=:now
                  AND (live_binding.ends_at IS NULL
                       OR live_binding.ends_at>:now)
              )
          AND charge.period_start>=:start AND charge.period_start<=:end
    """


def actual_cost(conn, *, now: datetime | None = None, days: int = 30) -> dict:
    """Return current-state actual cost for a UTC rolling window.

    Historical rows can remain in the database after a hard delete, but only
    rows whose live billing unit still exists are eligible here.
    """
    start, end = _window(now, days)
    params = {"start": start, "end": end, "now": end}
    metered = conn.execute(
        "SELECT COALESCE(SUM(r.billed_usage_cost),0) FROM request_log r "
        + _live_request_filter("r"), params
    ).fetchone()[0]
    proxy = conn.execute(
        "SELECT COALESCE(SUM(c.normalized_recurring_cost),0) "
        + _proxy_charge_sql(), params,
    ).fetchone()[0]
    agent = conn.execute(
        "SELECT COALESCE(SUM(allocation.normalized_recurring_cost),0) "
        + _agent_allocation_sql(), params,
    ).fetchone()[0]
    recurring = float(proxy or 0) + float(agent or 0)
    return {
        "metered_cost": float(metered or 0),
        "recurring_cost": recurring,
        "total_cost": float(metered or 0) + recurring,
    }


def recurring_by_period_start(conn, *, now: datetime | None = None,
                              days: int = 30) -> dict[str, float]:
    start, end = _window(now, days)
    params = {"start": start, "end": end, "now": end}
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
        "COALESCE(SUM(allocation.normalized_recurring_cost),0) cost "
        + _agent_allocation_sql() + "GROUP BY date(charge.period_start)", params,
    ):
        period_start, cost = row[0], row[1]
        result[period_start] = result.get(period_start, 0.0) + float(cost or 0)
    return result


def actual_cost_by_day(conn, *, now: datetime | None = None,
                       days: int = 30) -> dict[str, dict[str, float]]:
    """Return current-state metered/recurring/total cost by UTC day."""
    start, end = _window(now, days)
    params = {"start": start, "end": end, "now": end}
    result: dict[str, dict[str, float]] = {}

    for row in conn.execute(
        "SELECT date(r.requested_at) day,"
        "COALESCE(SUM(r.billed_usage_cost),0) cost "
        "FROM request_log r " + _live_request_filter("r") +
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
        "COALESCE(SUM(allocation.normalized_recurring_cost),0) cost "
        + _agent_allocation_sql() + "GROUP BY date(charge.period_start)", params):
        item = result.setdefault(str(row[0]), {
            "metered_cost": 0.0, "recurring_cost": 0.0,
        })
        item["recurring_cost"] += float(row[1] or 0)

    for item in result.values():
        item["cost"] = item["metered_cost"] + item["recurring_cost"]
    return result


__all__ = ["actual_cost", "actual_cost_by_day", "live_request_sql",
           "recurring_by_period_start"]
