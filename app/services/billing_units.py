"""Read-only resolution of billable lifecycle units.

The proxy contract and Agent subscription tables remain separate domains, but
their lifecycle identity is now represented by one explicit value object.
This module performs no writes and never opens another connection; callers
choose the transaction/profile that owns the read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from app.core.time import format_utc, parse_runtime_timestamp, utc_now
from app.db.proxy.common import _parse_iso_date


@dataclass(frozen=True)
class BillingUnit:
    """Stable identity and validity of one recurring charge unit."""

    billing_unit_id: str
    owner_id: int
    account_id: int | None
    owner_kind: str
    charge_domain: str
    valid_from: date
    ends_at: datetime | None
    currency: str
    credential_uuid: str | None = None
    credential_identity: str | None = None
    credential_runtime_id: int | None = None
    key_masked: str | None = None
    contract_id: int | None = None
    subscription_id: int | None = None

    @property
    def anchor_day(self) -> int:
        return self.valid_from.day


def _parsed_ends_at(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return parse_runtime_timestamp(value)


class BillingUnitResolver:
    """Resolve existing rows into immutable units using a caller's connection."""

    @staticmethod
    def proxy_units(conn, *, at: datetime | None = None) -> list[BillingUnit]:
        moment = utc_now() if at is None else at
        now = format_utc(moment)
        contracts = conn.execute(
            "SELECT bc.id,bc.uuid,bc.account_id,bc.billing_scope,bc.currency,"
            "bc.ends_at,a.valid_from account_valid_from,a.created_at account_created_at "
            "FROM billing_contracts bc JOIN accounts a ON a.id=bc.account_id "
            "WHERE bc.charge_type='recurring' AND bc.valid_from<=? "
            "AND a.account_kind='proxy'",
            (now,),
        ).fetchall()
        units: list[BillingUnit] = []
        for contract in contracts:
            ends_at = _parsed_ends_at(contract["ends_at"])
            if contract["billing_scope"] == "credential":
                seen: set[tuple[int, str]] = set()
                credentials = conn.execute(
                    "SELECT c.runtime_id,c.uuid,c.key_masked,c.valid_from,c.created_at,c.ends_at "
                    "FROM upstream_credentials c JOIN upstreams u ON u.id=c.upstream_id "
                    "WHERE u.account_id=? "
                    "AND (c.ends_at IS NULL OR c.ends_at>?) "
                    "ORDER BY c.position,c.runtime_id",
                    (contract["account_id"], now),
                ).fetchall()
                for credential in credentials:
                    identity = (contract["account_id"], credential["key_masked"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    valid_from = (
                        _parse_iso_date(credential["valid_from"])
                        or parse_runtime_timestamp(credential["created_at"]).date())
                    credential_end = _parsed_ends_at(credential["ends_at"])
                    unit_end = min((value for value in (ends_at, credential_end)
                                    if value is not None), default=None)
                    units.append(BillingUnit(
                        billing_unit_id=f"credential:{credential['uuid']}",
                        owner_id=int(contract["id"]),
                        account_id=int(contract["account_id"]),
                        owner_kind="proxy",
                        charge_domain="proxy_credential",
                        valid_from=valid_from,
                        ends_at=unit_end,
                        currency=str(contract["currency"]),
                        credential_uuid=str(credential["uuid"]),
                        credential_identity=f"{contract['account_id']}:{credential['key_masked']}",
                        credential_runtime_id=credential["runtime_id"],
                        key_masked=credential["key_masked"],
                        contract_id=int(contract["id"]),
                    ))
            else:
                valid_from = (
                    _parse_iso_date(contract["account_valid_from"])
                    or parse_runtime_timestamp(contract["account_created_at"]).date())
                units.append(BillingUnit(
                    billing_unit_id=f"contract:{contract['uuid']}",
                    owner_id=int(contract["id"]),
                    account_id=int(contract["account_id"]),
                    owner_kind="proxy",
                    charge_domain="proxy_subscription",
                    valid_from=valid_from,
                    ends_at=ends_at,
                    currency=str(contract["currency"]),
                    key_masked="subscription",
                    contract_id=int(contract["id"]),
                ))
        return units

    @staticmethod
    def agent_units(conn, *, at: datetime | None = None) -> list[BillingUnit]:
        moment = utc_now() if at is None else at
        now = format_utc(moment)
        rows = conn.execute(
            "SELECT i.id,i.uuid,i.subscription_id,i.valid_from,i.ends_at,s.currency "
            "FROM agent_subscription_instances i "
            "JOIN agent_subscriptions s ON s.id=i.subscription_id "
            "WHERE (s.ends_at IS NULL OR s.ends_at>=?) "
            "AND (i.ends_at IS NULL OR i.ends_at>=?) "
            "AND i.valid_from<=?", (now, now, now),
        ).fetchall()
        return [BillingUnit(
            billing_unit_id=f"agent-subscription-instance:{row['uuid']}",
            owner_id=int(row["id"]),
            account_id=None,
            owner_kind="agent",
            charge_domain="agent_subscription",
            valid_from=_parse_iso_date(str(row["valid_from"])[:10]),
            ends_at=_parsed_ends_at(row["ends_at"]),
            currency=str(row["currency"]),
            subscription_id=int(row["subscription_id"]),
        ) for row in rows]

    @staticmethod
    def end_stamp(unit: BillingUnit) -> str | None:
        return format_utc(unit.ends_at) if unit.ends_at else None


__all__ = ["BillingUnit", "BillingUnitResolver"]
