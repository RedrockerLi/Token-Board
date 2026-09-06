"""Immutable dashboard export events for finalized recurring charges."""

from __future__ import annotations

import sqlite3


def _insert_event(conn: sqlite3.Connection, row: dict) -> int:
    existing = conn.execute(
        "SELECT event_kind,source_table,source_key,account_id,account_uuid,"
        "account_name,account_kind,month,period_start,billing_unit_id,recurring_charge,"
        "normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_at "
        "FROM billing_export_events WHERE event_key=?", (row["event_key"],)
    ).fetchone()
    if existing is not None:
        fields = ("event_kind", "source_table", "source_key", "account_id",
                  "account_uuid", "account_name", "account_kind", "month", "period_start",
                  "billing_unit_id", "recurring_charge",
                  "normalized_recurring_cost", "currency", "base_currency",
                  "fx_rate_date", "frozen_at")
        if any(existing[field] != row[field] for field in fields):
            raise ValueError(
                f"billing event payload changed: {row['event_key']}")
        return 0
    cursor = conn.execute(
        """INSERT INTO billing_export_events
           (event_key,event_kind,source_table,source_key,account_id,account_uuid,
            account_name,account_kind,month,period_start,billing_unit_id,recurring_charge,
            normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_at)
           VALUES(:event_key,:event_kind,:source_table,:source_key,:account_id,
                   :account_uuid,:account_name,:account_kind,:month,:period_start,
                   :billing_unit_id,:recurring_charge,:normalized_recurring_cost,
                   :currency,:base_currency,:fx_rate_date,:frozen_at)""",
        row,
    )
    return max(cursor.rowcount, 0)


def append_proxy_billing_export_event(conn: sqlite3.Connection,
                                      charge_id: int) -> int:
    row = conn.execute(
        """SELECT c.id,c.period_start,c.billing_unit_id,c.credential_uuid,
                      c.contract_uuid_snapshot,c.recurring_charge,
                      c.normalized_recurring_cost,c.currency,c.base_currency,
                      c.fx_rate_date,c.finalized_at,
                      ai.id account_id,ai.uuid account_uuid,ai.name account_name,
                      ai.account_kind
               FROM billing_period_charges c
               JOIN account_identities ai ON ai.id=c.account_identity_id
               WHERE c.id=? AND c.finalized_at IS NOT NULL
                 AND ai.account_kind='proxy'""", (charge_id,)
    ).fetchone()
    if row is None:
        return 0
    unit_id = (row["billing_unit_id"] or row["credential_uuid"] or
               f"contract:{row['contract_uuid_snapshot'] or row['id']}")
    period_start = row["period_start"]
    return _insert_event(conn, {
        "event_key": f"proxy:{unit_id}:{period_start}",
        "event_kind": "proxy", "source_table": "billing_period_charges",
        "source_key": str(row["id"]), "account_id": row["account_id"],
        "account_uuid": row["account_uuid"], "account_name": row["account_name"],
        "account_kind": row["account_kind"], "month": str(period_start)[:7],
        "period_start": period_start,
        "billing_unit_id": unit_id,
        "recurring_charge": float(row["recurring_charge"] or 0),
        "normalized_recurring_cost": row["normalized_recurring_cost"],
        "currency": row["currency"] or "CNY",
        "base_currency": row["base_currency"] or "CNY",
        "fx_rate_date": row["fx_rate_date"], "frozen_at": row["finalized_at"],
    })


def append_agent_billing_export_event(conn: sqlite3.Connection,
                                      period_charge_id: int,
                                      software_id: int) -> int:
    row = conn.execute(
        """SELECT a.period_charge_id,a.software_id,a.recurring_charge,
                      a.normalized_recurring_cost,a.currency,a.base_currency,
                      a.fx_rate_date,a.finalized_at,c.period_start,
                      c.subscription_id,c.subscription_uuid_snapshot,
                      c.instance_uuid_snapshot,si.uuid subscription_identity_uuid,
                      ii.uuid instance_identity_uuid,s.uuid subscription_uuid,
                      i.uuid instance_uuid,ai.id account_id,ai.uuid account_uuid,
                      ai.name account_name,ai.account_kind
               FROM agent_subscription_charge_allocations a
               JOIN agent_subscription_period_charges c ON c.id=a.period_charge_id
               LEFT JOIN agent_subscription_instances i ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               LEFT JOIN agent_subscription_instance_identities ii ON ii.id=c.instance_id
               LEFT JOIN agent_subscription_identities si ON si.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=a.software_id
               WHERE a.period_charge_id=? AND a.software_id=?
                 AND c.finalized_at IS NOT NULL AND a.finalized_at IS NOT NULL
                 AND ai.account_kind='agent'""", (period_charge_id, software_id)
    ).fetchone()
    if row is None:
        return 0
    subscription_uuid = (row["subscription_uuid_snapshot"] or
                         row["subscription_identity_uuid"] or
                         row["subscription_uuid"] or f"subscription:{row['subscription_id']}")
    instance_uuid = (row["instance_uuid_snapshot"] or row["instance_identity_uuid"] or
                     row["instance_uuid"] or f"instance:{row['period_charge_id']}")
    period_start = row["period_start"]
    source_key = f"{period_charge_id}:{software_id}"
    unit_id = f"agent-subscription-instance:{instance_uuid}"
    return _insert_event(conn, {
        "event_key": f"agent:{subscription_uuid}:{instance_uuid}:{row['account_uuid'] or software_id}:{period_start}",
        "event_kind": "agent",
        "source_table": "agent_subscription_charge_allocations",
        "source_key": source_key, "account_id": row["account_id"],
        "account_uuid": row["account_uuid"], "account_name": row["account_name"],
        "account_kind": row["account_kind"], "month": str(period_start)[:7],
        "period_start": period_start,
        "billing_unit_id": unit_id,
        "recurring_charge": float(row["recurring_charge"] or 0),
        "normalized_recurring_cost": row["normalized_recurring_cost"],
        "currency": row["currency"] or "CNY",
        "base_currency": row["base_currency"] or "CNY",
        "fx_rate_date": row["fx_rate_date"], "frozen_at": row["finalized_at"],
    })


def append_billing_export_events_for_finalized_at(conn: sqlite3.Connection,
                                                  finalized_at: str) -> int:
    """Append only facts finalized by the current materialization transaction."""
    created = 0
    for row in conn.execute(
            "SELECT id FROM billing_period_charges WHERE finalized_at=?",
            (finalized_at,)):
        created += append_proxy_billing_export_event(conn, int(row["id"]))
    for row in conn.execute(
            "SELECT period_charge_id,software_id "
            "FROM agent_subscription_charge_allocations WHERE finalized_at=?",
            (finalized_at,)):
        created += append_agent_billing_export_event(
            conn, int(row["period_charge_id"]), int(row["software_id"]))
    return created


def ensure_billing_export_events_conn(conn: sqlite3.Connection) -> int:
    """Create one immutable export event for every finalized billing fact.

    This function is idempotent and must run on the same caller-owned
    transaction that finalizes billing facts.  It deliberately snapshots the
    dashboard payload so export never has to reconstruct a deleted contract,
    credential, or subscription.
    """
    created = 0
    existing_agent_sources = {
        row["source_key"] for row in conn.execute(
            "SELECT source_key FROM billing_export_events "
            "WHERE source_table='agent_subscription_charge_allocations'"
        ).fetchall()
    }
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
            "period_start": period_start,
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
                      c.period_start,c.instance_id,c.subscription_id,
                      c.subscription_uuid_snapshot,c.instance_uuid_snapshot,
                      si.uuid subscription_identity_uuid,
                      ii.uuid instance_identity_uuid,
                      s.uuid subscription_uuid,i.uuid instance_uuid,
                      ai.id account_id,ai.uuid account_uuid,ai.name account_name,
                      ai.account_kind
               FROM agent_subscription_charge_allocations a
               JOIN agent_subscription_period_charges c
                 ON c.id=a.period_charge_id
               LEFT JOIN agent_subscription_instances i
                 ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s
                 ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               LEFT JOIN agent_subscription_instance_identities ii
                 ON ii.id=c.instance_id
               LEFT JOIN agent_subscription_identities si
                 ON si.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=a.software_id
               WHERE c.finalized_at IS NOT NULL
                 AND a.finalized_at IS NOT NULL
                 AND ai.account_kind='agent'
               ORDER BY a.period_charge_id,a.software_id"""):
        subscription_uuid = (row["subscription_uuid_snapshot"] or
                             row["subscription_identity_uuid"] or
                             row["subscription_uuid"] or
                             f"subscription:{row['subscription_id']}")
        instance_uuid = (row["instance_uuid_snapshot"] or
                         row["instance_identity_uuid"] or
                         row["instance_uuid"] or
                         f"instance:{row['instance_id']}")
        software_uuid = row["account_uuid"] or str(row["software_id"])
        period_start = row["period_start"]
        source_key = f"{row['period_charge_id']}:{row['software_id']}"
        # V1.18 seeded legacy event keys without the instance identity.  The
        # source key is the immutable allocation identity, so reuse that
        # event instead of creating a second row after V1.19.
        if source_key in existing_agent_sources:
            continue
        unit_id = f"agent-subscription-instance:{instance_uuid}"
        created += _insert_event(conn, {
            "event_key": (
                f"agent:{subscription_uuid}:{instance_uuid}:"
                f"{software_uuid}:{period_start}"
            ),
            "event_kind": "agent",
            "source_table": "agent_subscription_charge_allocations",
            "source_key": source_key,
            "account_id": row["account_id"],
            "account_uuid": row["account_uuid"],
            "account_name": row["account_name"],
            "account_kind": row["account_kind"],
            "month": str(period_start)[:7],
            "period_start": period_start,
            "billing_unit_id": unit_id,
            "recurring_charge": float(row["recurring_charge"] or 0),
            "normalized_recurring_cost": row["normalized_recurring_cost"],
            "currency": row["currency"] or "CNY",
            "base_currency": row["base_currency"] or "CNY",
            "fx_rate_date": row["fx_rate_date"],
            "frozen_at": row["finalized_at"],
        })
        existing_agent_sources.add(source_key)
    return created
