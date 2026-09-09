"""Immutable dashboard export events for finalized recurring charges."""

from __future__ import annotations

import sqlite3


_PAYLOAD_FIELDS = (
    "event_kind", "source_table", "source_key", "account_id", "account_uuid",
    "account_name", "account_kind", "month", "period_start", "billing_unit_id",
    "recurring_charge", "normalized_recurring_cost", "currency", "base_currency",
    "fx_rate_date", "frozen_on",
)


def _same_payload(existing: sqlite3.Row, row: dict) -> bool:
    return all(existing[field] == row[field] for field in _PAYLOAD_FIELDS)


def _same_payload_with_legacy_agent_unit(existing: sqlite3.Row,
                                         row: dict) -> bool:
    """Accept the V1 Agent unit label while recovering the same source fact.

    V1 events used the subscription identity as their display unit, while
    V2.1 derives the unit from the subscription-instance identity.  The
    historical allocation source key is unchanged across that migration. Keep the
    already-exported V1 payload authoritative, but continue to reject every
    other payload mutation.
    """
    if _same_payload(existing, row):
        return True
    if not (
        existing["event_kind"] == row["event_kind"] == "agent"
        and str(existing["billing_unit_id"] or "").startswith(
            "agent-subscription:"
        )
        and str(row["billing_unit_id"] or "").startswith(
            "agent-subscription-instance:"
        )
    ):
        return False
    return all(
        existing[field] == row[field]
        for field in _PAYLOAD_FIELDS
        if field != "billing_unit_id"
    )


def _insert_event(conn: sqlite3.Connection, row: dict) -> int:
    """Insert one event using ``(source_table, source_key)`` as its idempotency key.

    ``event_key`` remains a stable logical identifier for callers and old
    artifacts, but a source fact must never get a second event merely because
    its logical key was reconstructed differently after a deletion or sync.
    """
    existing = conn.execute(
        "SELECT " + ",".join(_PAYLOAD_FIELDS) +
        " FROM billing_export_events WHERE source_table=? AND source_key=?",
        (row["source_table"], row["source_key"]),
    ).fetchone()
    if existing is not None:
        if not _same_payload_with_legacy_agent_unit(existing, row):
            raise ValueError(
                "billing export source payload changed: "
                f"{row['source_table']}:{row['source_key']}"
            )
        return 0

    existing = conn.execute(
        "SELECT " + ",".join(_PAYLOAD_FIELDS) +
        " FROM billing_export_events WHERE event_key=?", (row["event_key"],)
    ).fetchone()
    if existing is not None:
        if not _same_payload(existing, row):
            raise ValueError(
                f"billing event payload changed: {row['event_key']}"
            )
        return 0

    cursor = conn.execute(
        """INSERT INTO billing_export_events
           (event_key,event_kind,source_table,source_key,account_id,account_uuid,
            account_name,account_kind,month,period_start,billing_unit_id,recurring_charge,
            normalized_recurring_cost,currency,base_currency,fx_rate_date,frozen_on)
           VALUES(:event_key,:event_kind,:source_table,:source_key,:account_id,
                   :account_uuid,:account_name,:account_kind,:month,:period_start,
                   :billing_unit_id,:recurring_charge,:normalized_recurring_cost,
                   :currency,:base_currency,:fx_rate_date,:frozen_on)""",
        row,
    )
    return max(cursor.rowcount, 0)


def _date_value(value: object) -> str:
    text = str(value or "")
    return text[:10]


def _proxy_event_row(row: sqlite3.Row) -> dict:
    unit_id = (row["billing_unit_id"] or row["credential_uuid"] or
               f"contract:{row['contract_uuid_snapshot'] or row['id']}")
    period_start = row["period_start"]
    return {
        "event_key": f"proxy:{unit_id}:{period_start}",
        "event_kind": "proxy", "source_table": "billing_period_charges",
        "source_key": str(row["id"]), "account_id": row["account_id"],
        "account_uuid": row["account_uuid"], "account_name": row["account_name"],
        "account_kind": row["account_kind"], "month": str(period_start)[:7],
        "period_start": period_start, "billing_unit_id": unit_id,
        "recurring_charge": float(row["recurring_charge"] or 0),
        "normalized_recurring_cost": row["normalized_recurring_cost"],
        "currency": row["currency"] or "CNY",
        "base_currency": row["base_currency"] or "CNY",
        "fx_rate_date": row["fx_rate_date"],
        "frozen_on": _date_value(row["finalized_at"]),
    }


def _agent_event_row(row: sqlite3.Row) -> dict:
    """Build an export event for a legacy allocation row."""
    subscription_uuid = (row["subscription_uuid_snapshot"] or
                         row["subscription_identity_uuid"] or
                         row["subscription_uuid"] or
                         f"subscription:{row['subscription_id']}")
    instance_uuid = (row["instance_uuid_snapshot"] or
                     row["instance_identity_uuid"] or row["instance_uuid"] or
                     f"instance:{row['instance_id']}")
    period_start = row["period_start"]
    source_key = f"{row['period_charge_id']}:{row['software_id']}"
    unit_id = f"agent-subscription-instance:{instance_uuid}"
    return {
        "event_key": (
            f"agent:{subscription_uuid}:{instance_uuid}:"
            f"{row['account_uuid'] or row['software_id']}:{period_start}"
        ),
        "event_kind": "agent",
        "source_table": "agent_subscription_charge_allocations",
        "source_key": source_key, "account_id": row["account_id"],
        "account_uuid": row["account_uuid"], "account_name": row["account_name"],
        "account_kind": row["account_kind"], "month": str(period_start)[:7],
        "period_start": period_start, "billing_unit_id": unit_id,
        "recurring_charge": float(row["recurring_charge"] or 0),
        "normalized_recurring_cost": row["normalized_recurring_cost"],
        "currency": row["currency"] or "CNY",
        "base_currency": row["base_currency"] or "CNY",
        "fx_rate_date": row["fx_rate_date"],
        "frozen_on": _date_value(row["finalized_on"]),
    }


def _direct_agent_event_row(row: sqlite3.Row) -> dict:
    """Build an export event directly from one owned period charge."""
    subscription_uuid = (row["subscription_uuid_snapshot"] or
                         row["subscription_identity_uuid"] or
                         row["subscription_uuid"] or
                         f"subscription:{row['subscription_id']}")
    instance_uuid = (row["instance_uuid_snapshot"] or
                     row["instance_identity_uuid"] or row["instance_uuid"] or
                     f"instance:{row['instance_id']}")
    period_start = row["period_start"]
    return {
        "event_key": (
            f"agent:{subscription_uuid}:{instance_uuid}:"
            f"{row['account_uuid'] or row['software_id']}:{period_start}"
        ),
        "event_kind": "agent",
        "source_table": "agent_subscription_period_charges",
        "source_key": str(row["period_charge_id"]),
        "account_id": row["account_id"],
        "account_uuid": row["account_uuid"],
        "account_name": row["account_name"],
        "account_kind": row["account_kind"],
        "month": str(period_start)[:7],
        "period_start": period_start,
        "billing_unit_id": f"agent-subscription-instance:{instance_uuid}",
        "recurring_charge": float(row["recurring_charge"] or 0),
        "normalized_recurring_cost": row["normalized_recurring_cost"],
        "currency": row["currency"] or "CNY",
        "base_currency": row["base_currency"] or "CNY",
        "fx_rate_date": row["fx_rate_date"],
        "frozen_on": _date_value(row["finalized_on"]),
    }


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
    return _insert_event(conn, _proxy_event_row(row))


def append_agent_billing_export_event(conn: sqlite3.Connection,
                                      period_charge_id: int,
                                      software_id: int) -> int:
    row = conn.execute(
        """SELECT a.period_charge_id,a.software_id,a.recurring_charge,
                      a.normalized_recurring_cost,a.currency,a.base_currency,
                      a.fx_rate_date,a.finalized_on,c.period_start,c.instance_id,
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
                 AND c.is_finalized=1 AND a.is_finalized=1
                 AND ai.account_kind='agent'""", (period_charge_id, software_id)
    ).fetchone()
    if row is None:
        return 0
    return _insert_event(conn, _agent_event_row(row))


def append_direct_agent_billing_export_event(conn: sqlite3.Connection,
                                             period_charge_id: int) -> int:
    """Append the event for a directly-owned finalized period charge."""
    row = conn.execute(
        """SELECT c.id period_charge_id,c.software_id,c.recurring_charge,
                      c.normalized_recurring_cost,c.currency,c.base_currency,
                      c.fx_rate_date,c.finalized_on,c.period_start,c.instance_id,
                      c.subscription_id,c.subscription_uuid_snapshot,
                      c.instance_uuid_snapshot,si.uuid subscription_identity_uuid,
                      ii.uuid instance_identity_uuid,s.uuid subscription_uuid,
                      i.uuid instance_uuid,ai.id account_id,ai.uuid account_uuid,
                      ai.name account_name,ai.account_kind
               FROM agent_subscription_period_charges c
               LEFT JOIN agent_subscription_instances i ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               LEFT JOIN agent_subscription_instance_identities ii ON ii.id=c.instance_id
               LEFT JOIN agent_subscription_identities si ON si.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=c.software_id
               WHERE c.id=? AND c.software_id IS NOT NULL
                 AND c.is_finalized=1 AND ai.account_kind='agent'""",
        (period_charge_id,),
    ).fetchone()
    if row is None:
        return 0
    return _insert_event(conn, _direct_agent_event_row(row))


def ensure_billing_export_events_conn(conn: sqlite3.Connection) -> int:
    """Ensure every finalized billing fact has exactly one export event.

    New Agent events use the period charge itself as the immutable source. The
    allocation scan remains only for historical V2.1 rows and never receives
    new records.
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
               ORDER BY c.id"""
    ):
        created += _insert_event(conn, _proxy_event_row(row))

    for row in conn.execute(
        """SELECT c.id period_charge_id,c.software_id,c.recurring_charge,
                      c.normalized_recurring_cost,c.currency,c.base_currency,
                      c.fx_rate_date,c.finalized_on,c.period_start,c.instance_id,
                      c.subscription_id,c.subscription_uuid_snapshot,
                      c.instance_uuid_snapshot,si.uuid subscription_identity_uuid,
                      ii.uuid instance_identity_uuid,s.uuid subscription_uuid,
                      i.uuid instance_uuid,ai.id account_id,ai.uuid account_uuid,
                      ai.name account_name,ai.account_kind
               FROM agent_subscription_period_charges c
               LEFT JOIN agent_subscription_instances i ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               LEFT JOIN agent_subscription_instance_identities ii ON ii.id=c.instance_id
               LEFT JOIN agent_subscription_identities si ON si.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=c.software_id
               WHERE c.software_id IS NOT NULL AND c.is_finalized=1
                 AND ai.account_kind='agent'
               ORDER BY c.id"""
    ):
        created += _insert_event(conn, _direct_agent_event_row(row))

    for row in conn.execute(
        """SELECT a.period_charge_id,a.software_id,a.recurring_charge,
                      a.normalized_recurring_cost,a.currency,a.base_currency,
                      a.fx_rate_date,a.finalized_on,
                      c.period_start,c.instance_id,c.subscription_id,
                      c.subscription_uuid_snapshot,c.instance_uuid_snapshot,
                      si.uuid subscription_identity_uuid,
                      ii.uuid instance_identity_uuid,
                      s.uuid subscription_uuid,i.uuid instance_uuid,
                      ai.id account_id,ai.uuid account_uuid,ai.name account_name,
                      ai.account_kind
               FROM agent_subscription_charge_allocations a
               JOIN agent_subscription_period_charges c ON c.id=a.period_charge_id
               LEFT JOIN agent_subscription_instances i ON i.id=c.instance_id
               LEFT JOIN agent_subscriptions s ON s.id=COALESCE(c.subscription_id,i.subscription_id)
               LEFT JOIN agent_subscription_instance_identities ii ON ii.id=c.instance_id
               LEFT JOIN agent_subscription_identities si ON si.id=COALESCE(c.subscription_id,i.subscription_id)
               JOIN account_identities ai ON ai.id=a.software_id
               WHERE c.software_id IS NULL
                 AND c.is_finalized=1 AND a.is_finalized=1
                 AND ai.account_kind='agent'
               ORDER BY a.period_charge_id,a.software_id"""
    ):
        created += _insert_event(conn, _agent_event_row(row))
    return created
