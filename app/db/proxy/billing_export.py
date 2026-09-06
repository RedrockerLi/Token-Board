"""Immutable dashboard export events for finalized recurring charges."""

from __future__ import annotations

import sqlite3


def _insert_event(conn: sqlite3.Connection, row: dict) -> int:
    cursor = conn.execute(
        """INSERT OR IGNORE INTO billing_export_events
           (event_key,event_kind,source_table,source_key,account_id,account_uuid,
            account_name,account_kind,month,billing_unit_id,recurring_charge,
            normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_at)
           VALUES(:event_key,:event_kind,:source_table,:source_key,:account_id,
                   :account_uuid,:account_name,:account_kind,:month,
                   :billing_unit_id,:recurring_charge,:normalized_recurring_cost,
                   :currency,:base_currency,:fx_rate_date,:frozen_at)""",
        row,
    )
    return max(cursor.rowcount, 0)


def ensure_billing_export_events_conn(conn: sqlite3.Connection) -> int:
    """Create one immutable export event for every finalized billing fact.

    This function is idempotent and must run on the same caller-owned
    transaction that finalizes billing facts.  It deliberately snapshots the
    dashboard payload so export never has to reconstruct a deleted contract,
    credential, or subscription.
    """
    created = 0
    for row in conn.execute(
        """SELECT c.id,c.period_start,c.billing_unit_id,c.credential_uuid,
                      c.contract_uuid_snapshot,c.recurring_charge,
                      c.normalized_recurring_cost,c.currency,c.base_currency,
                      c.fx_rate_date,c.finalized_at,
                      ai.id account_id,ai.uuid account_uuid,ai.name account_name,
                      ai.account_kind
               FROM billing_period_charges c
               JOIN account_identities ai
                 ON ai.id=COALESCE(
                      c.account_identity_id,
                      (SELECT bc.account_id FROM billing_contracts bc
                       WHERE bc.id=c.contract_id))
               WHERE c.finalized_at IS NOT NULL
                 AND ai.account_kind='proxy'
               ORDER BY c.id"""):
        unit_id = (row["billing_unit_id"] or row["credential_uuid"] or
                   f"contract:{row['contract_uuid_snapshot'] or row['id']}")
        period_start = row["period_start"]
        created += _insert_event(conn, {
            "event_key": f"proxy:{unit_id}:{period_start}",
            "event_kind": "proxy",
            "source_table": "billing_period_charges",
            "source_key": str(row["id"]),
            "account_id": row["account_id"],
            "account_uuid": row["account_uuid"],
            "account_name": row["account_name"],
            "account_kind": row["account_kind"],
            "month": str(period_start)[:7],
            "billing_unit_id": unit_id,
            "recurring_charge": float(row["recurring_charge"] or 0),
            "normalized_recurring_cost": row["normalized_recurring_cost"],
            "currency": row["currency"] or "CNY",
            "base_currency": row["base_currency"] or "CNY",
            "fx_rate_date": row["fx_rate_date"],
            "frozen_at": row["finalized_at"],
        })

    for row in conn.execute(
        """SELECT a.period_charge_id,a.software_id,a.recurring_charge,
                      a.normalized_recurring_cost,a.currency,a.base_currency,
                      a.fx_rate_date,a.finalized_at,
                      c.period_start,c.subscription_uuid_snapshot,
                      c.subscription_id,s.uuid subscription_uuid,
                      ai.id account_id,ai.uuid account_uuid,ai.name account_name,
                      ai.account_kind
               FROM agent_subscription_charge_allocations a
               JOIN agent_subscription_period_charges c
                 ON c.id=a.period_charge_id
               LEFT JOIN agent_subscription_instances i
                 ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s
                 ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=a.software_id
               WHERE c.finalized_at IS NOT NULL
                 AND a.finalized_at IS NOT NULL
                 AND ai.account_kind='agent'
               ORDER BY a.period_charge_id,a.software_id"""):
        subscription_uuid = (row["subscription_uuid_snapshot"] or
                             row["subscription_uuid"] or
                             f"subscription:{row['subscription_id']}")
        software_uuid = row["account_uuid"] or str(row["software_id"])
        period_start = row["period_start"]
        unit_id = f"agent-subscription:{subscription_uuid}"
        created += _insert_event(conn, {
            "event_key": f"agent:{subscription_uuid}:{software_uuid}:{period_start}",
            "event_kind": "agent",
            "source_table": "agent_subscription_charge_allocations",
            "source_key": f"{row['period_charge_id']}:{row['software_id']}",
            "account_id": row["account_id"],
            "account_uuid": row["account_uuid"],
            "account_name": row["account_name"],
            "account_kind": row["account_kind"],
            "month": str(period_start)[:7],
            "billing_unit_id": unit_id,
            "recurring_charge": float(row["recurring_charge"] or 0),
            "normalized_recurring_cost": row["normalized_recurring_cost"],
            "currency": row["currency"] or "CNY",
            "base_currency": row["base_currency"] or "CNY",
            "fx_rate_date": row["fx_rate_date"],
            "frozen_at": row["finalized_at"],
        })
    return created
