"""Read-only resolution of billable lifecycle units.

The proxy contract and Agent subscription tables remain separate domains, but
their lifecycle identity is now represented by one explicit value object.
This module performs no writes and never opens another connection; callers
choose the transaction/profile that owns the read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from app.core.time import as_utc, format_utc, parse_runtime_timestamp, utc_now
from app.db.proxy.common import _parse_iso_date


# A usage snapshot older than this cannot drive a reset-driven period: the
# upstream may have rolled since, so billing falls back to the anchor day.
OBSERVED_PERIOD_MAX_AGE_SECONDS = 48 * 3600


@dataclass(frozen=True)
class ObservedPeriod:
    """Complete monthly period reported by the upstream usage endpoint."""

    start: datetime
    end: datetime
    checked_at: datetime

    def fresh(self, moment: datetime,
              max_age: int = OBSERVED_PERIOD_MAX_AGE_SECONDS) -> bool:
        """Whether this observation is recent enough to drive billing."""
        age = (as_utc(moment) - self.checked_at).total_seconds()
        return abs(age) <= max_age


def _optional_timestamp(value: object) -> datetime | None:
    """Parse one optional runtime timestamp, ignoring malformed values."""
    if value in (None, ""):
        return None
    try:
        return parse_runtime_timestamp(value)
    except ValueError:
        return None


def parse_observed_period(raw: object) -> ObservedPeriod | None:
    """Parse ``upstream_credentials.usage_json`` into its observed period.

    An errored, malformed or incomplete-monthly snapshot yields None, which
    makes the unit fall back to its anchor-day period.  Both ends of the
    monthly period are required: a snapshot that names only a start would
    otherwise pin the first billing period to the moment it was taken.
    """
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    monthly = payload.get("monthly_period")
    checked_at = _optional_timestamp(payload.get("checked_at"))
    if checked_at is None or not isinstance(monthly, dict):
        return None
    start = _optional_timestamp(monthly.get("start"))
    end = _optional_timestamp(monthly.get("end"))
    if start is None or end is None:
        return None
    return ObservedPeriod(start=start, end=end, checked_at=checked_at)


def observed_period_end(raw: object, moment: datetime, *,
                        max_age: int = OBSERVED_PERIOD_MAX_AGE_SECONDS
                        ) -> datetime | None:
    """A fresh observation's monthly end, or None for the anchor fallback."""
    observed = parse_observed_period(raw)
    if observed is None or not observed.fresh(moment, max_age):
        return None
    return observed.end


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
    ends_on: date | None = None
    credential_uuid: str | None = None
    credential_identity: str | None = None
    credential_runtime_id: int | None = None
    key_masked: str | None = None
    contract_id: int | None = None
    subscription_id: int | None = None
    observed_period: ObservedPeriod | None = None

    @property
    def anchor_day(self) -> int:
        return self.valid_from.day

    def observed_fresh(self, moment: datetime,
                       max_age: int = OBSERVED_PERIOD_MAX_AGE_SECONDS) -> bool:
        """Whether this unit carries a usable observed monthly period."""
        return (self.observed_period is not None
                and self.observed_period.fresh(moment, max_age))


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
                    "SELECT c.runtime_id,c.uuid,c.key_masked,c.valid_from,c.created_at,"
                    "c.ends_at,c.usage_json "
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
                        observed_period=parse_observed_period(
                            credential["usage_json"]),
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
    def agent_units(conn, *, at: datetime | None = None,
                    include_ended: bool = False) -> list[BillingUnit]:
        moment = utc_now() if at is None else at
        today = moment.date().isoformat()
        visibility = ""
        params: list[object] = [today, today, today]
        if not include_ended:
            visibility = (
                " AND (s.ends_on IS NULL OR s.ends_on>?)"
                " AND (i.ends_on IS NULL OR i.ends_on>?)"
            )
            params.extend((today, today))
        rows = conn.execute(
            "SELECT i.id,i.uuid,i.subscription_id,i.valid_from,i.ends_on,s.currency,"
            "live_binding.software_id "
            "FROM agent_subscription_instances i "
            "JOIN agent_subscriptions s ON s.id=i.subscription_id "
            "JOIN agent_subscription_bindings live_binding "
            "ON live_binding.id=("
            "SELECT b.id FROM agent_subscription_bindings b "
            "WHERE b.subscription_id=s.id AND b.valid_from<=? "
            "AND (b.ends_on IS NULL OR b.ends_on>?) "
            "ORDER BY b.valid_from DESC,b.id DESC LIMIT 1) "
            "JOIN agent_software live_software ON live_software.id=live_binding.software_id "
            "JOIN accounts live_agent_account "
            "ON live_agent_account.id=live_software.id "
            "AND live_agent_account.account_kind='agent' "
            "WHERE i.valid_from<=?" + visibility,
            tuple(params),
        ).fetchall()
        return [BillingUnit(
            billing_unit_id=f"agent-subscription-instance:{row['uuid']}",
            owner_id=int(row["id"]),
            account_id=int(row["software_id"]),
            owner_kind="agent",
            charge_domain="agent_subscription",
            valid_from=_parse_iso_date(str(row["valid_from"])[:10]),
            ends_at=None,
            currency=str(row["currency"]),
            ends_on=_parse_iso_date(str(row["ends_on"])[:10])
            if row["ends_on"] else None,
            subscription_id=int(row["subscription_id"]),
        ) for row in rows]

    @staticmethod
    def end_stamp(unit: BillingUnit) -> str | None:
        return format_utc(unit.ends_at) if unit.ends_at else None


__all__ = [
    "OBSERVED_PERIOD_MAX_AGE_SECONDS", "BillingUnit", "BillingUnitResolver",
    "ObservedPeriod", "observed_period_end", "parse_observed_period",
]
