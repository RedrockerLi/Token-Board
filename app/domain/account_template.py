"""AccountTemplateAdapter — derives the API-facing account shape.

``account_type`` and ``is_aggregate`` are UI/template compatibility values,
not storage-table identities.  This adapter is the single place that derives
them from the normalized V1 records the application actually stores, so the
four shapes (api / plan / agent / aggregate) are produced from one routine
instead of being re-derived inside SQL projections in several callers.

The behavioral spec of each type stays in :data:`account_types.ACCOUNT_TYPES`;
this adapter maps normalized rows onto that spec and onto the ``is_aggregate``
flag.  It never probes V0 table layouts — it only reads the normalized V1
rows that ``accounts_read``/``lifecycle`` already query.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountTemplate:
    """The API-facing projection of one account record.

    All fields mirror the existing HTTP contract exactly; the adapter adds
    nothing new, it simply centralizes the derivation that previously lived in
    SQL ``CASE`` blocks and route-set joins.
    """

    id: int
    name: str
    base_url: str = ""
    api_format: str = "openai"
    endpoint_path: str = ""
    auth_header: str = "auto"
    is_aggregate: bool = False
    account_type: str = "api"
    monthly_price: float = 0.0
    currency: str = "CNY"
    agent_kind: str = ""
    valid_from: str = ""
    max_concurrency: int = 0
    created_at: str = ""
    deleted_at: str | None = None
    key_count: int = 0
    keys: list[dict] = field(default_factory=list)
    cloud_keys: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_format": self.api_format,
            "endpoint_path": self.endpoint_path,
            "auth_header": self.auth_header,
            "is_aggregate": 1 if self.is_aggregate else 0,
            "account_type": self.account_type,
            "monthly_price": self.monthly_price,
            "currency": self.currency,
            "agent_kind": self.agent_kind,
            "valid_from": self.valid_from,
            "max_concurrency": self.max_concurrency,
            "created_at": self.created_at,
            "deleted_at": self.deleted_at,
            "key_count": self.key_count,
            "keys": self.keys,
            "cloud_keys": self.cloud_keys,
        }


class AccountTemplateAdapter:
    """Map normalized V1 rows onto the api/plan/agent/aggregate templates.

    Each shape is derived from canonical records only:

    - ``api``      — account + metered contract + upstream + route set
    - ``plan``     — account + recurring contract + upstream + route set
    - ``agent``    — account + recurring contract + importer (no upstream)
    - ``aggregate``— route set + route rules, not a billing entity

    The derivation mirrors the historical SQL ``CASE`` exactly so the HTTP
    contract is unchanged: recurring + importer → agent, recurring → plan,
    otherwise api.
    """

    def routed(self, row: dict, *, recurring: bool, importer_kind: str) -> AccountTemplate:
        """An account that owns an upstream (api/plan, or agent-with-upstream)."""
        account_type = "agent" if (recurring and importer_kind) else (
            "plan" if recurring else "api")
        return AccountTemplate(
            id=int(row["id"]),
            name=row["name"],
            base_url=row.get("base_url") or "",
            api_format=row.get("api_format") or "openai",
            endpoint_path=row.get("endpoint_path") or "",
            auth_header=row.get("auth_header") or "auto",
            is_aggregate=False,
            account_type=account_type,
            monthly_price=float(row.get("recurring_price") or 0),
            currency=row.get("currency") or "CNY",
            agent_kind=importer_kind or "",
            valid_from=row.get("valid_from") or "",
            max_concurrency=int(row.get("max_concurrency") or 0),
            created_at=row.get("created_at"),
            deleted_at=row.get("deleted_at"),
            key_count=int(row.get("key_count") or 0),
        )

    def agent_only(self, row: dict, importer_kind: str, recurring_price: float,
                   currency: str) -> AccountTemplate:
        """An importer-only account with no upstream/credential."""
        return AccountTemplate(
            id=int(row["id"]),
            name=row["name"],
            is_aggregate=False,
            account_type="agent",
            monthly_price=float(recurring_price or 0),
            currency=currency or "CNY",
            agent_kind=importer_kind or "",
            valid_from=row.get("valid_from") or "",
            created_at=row.get("created_at"),
            deleted_at=row.get("deleted_at"),
        )

    def aggregate(self, row: dict, entries: list[dict]) -> AccountTemplate:
        """A route set with no billing account behind it (is_aggregate=1)."""
        return AccountTemplate(
            id=int(row["id"]),
            name=row["name"],
            is_aggregate=True,
            account_type="aggregate",
            created_at=row.get("created_at"),
            deleted_at=row.get("deleted_at"),
            keys=entries,
        )
