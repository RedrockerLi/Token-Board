"""Upstream account-type semantics — the single source of truth.

`account_type` is a UI/template compatibility value, not a storage-table
identity. Its behavior is defined HERE, as a declarative spec table: what an
account of a given type may do (routing / keys / billing / cooldown / deletion
/ usage source). Code must ask the spec (``spec(t).routable`` …) instead of
comparing the string directly, so adding a type is one row in
:data:`ACCOUNT_TYPES` — not a new branch in every caller.

The C++ proxy mirrors the parts it needs in ``proxy/src/core/account_types.h``
(non-routable filter) — keep the two in sync.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountTypeSpec:
    """Behavioral contract of one upstream account type."""

    billing: str  # "usage"（按量，api_cost=真实账单）| "subscription"（月费，api_cost=虚拟）
    routable: bool  # 可被本地密钥绑定 / 被代理转发
    holds_keys: bool  # 持有上游密钥（每把 key = 一个并发槽位/冷却单元）
    usage_source: str  # "proxy"（代理转发自动记账）| "import"（后台导入）
    deletion: str  # "immediate"（始终立即删除）| "configurable"（immediate|end_of_period）
    cooldown: str | None  # "subscription_5h"（明确配额429→5h冷却）| None
    subscription_unit: str | None  # "per_key" | "per_account" | None（非订阅类型）
    label: str  # 设置页类型下拉文案
    short_label: str  # 账户列表徽章文案

    def to_dict(self) -> dict:
        return {
            "billing": self.billing,
            "routable": self.routable,
            "holds_keys": self.holds_keys,
            "usage_source": self.usage_source,
            "deletion": self.deletion,
            "cooldown": self.cooldown,
            "subscription_unit": self.subscription_unit,
            "label": self.label,
            "short_label": self.short_label,
        }


# Only routable proxy upstream types live in this table.  Agent software is
# managed by agent_software and must never be exposed as an upstream type.
ACCOUNT_TYPES: dict[str, AccountTypeSpec] = {
    "api": AccountTypeSpec(
        billing="usage",
        routable=True,
        holds_keys=True,
        usage_source="proxy",
        deletion="immediate",
        cooldown=None,
        subscription_unit=None,
        label="api — 按调用量计费",
        short_label="API",
    ),
    "plan": AccountTypeSpec(
        billing="subscription",
        routable=True,
        holds_keys=True,
        usage_source="proxy",
        deletion="configurable",
        cooldown="subscription_5h",
        subscription_unit="per_key",
        label="plan — 订阅套餐，调用免费",
        short_label="Plan",
    ),
}


def spec(account_type: str | None) -> AccountTypeSpec:
    """Spec for a type string; unknown/empty falls back to api.

    Mirrors the SQL convention ``COALESCE(account_type,'api')`` — an unknown
    type must never be dropped or treated as routable-by-default elsewhere.
    """
    return ACCOUNT_TYPES.get(account_type or "api", ACCOUNT_TYPES["api"])


def is_routable(account_type: str | None) -> bool:
    return spec(account_type).routable


def holds_keys(account_type: str | None) -> bool:
    return spec(account_type).holds_keys


def is_subscription(account_type: str | None) -> bool:
    return spec(account_type).billing == "subscription"


def deletion_policy(account_type: str | None) -> str:
    return spec(account_type).deletion


def _types_with(predicate) -> tuple[str, ...]:
    return tuple(t for t, s in ACCOUNT_TYPES.items() if predicate(s))


def usage_billed_types() -> tuple[str, ...]:
    """account types whose api_cost is the real bill (currently api)."""
    return _types_with(lambda s: s.billing == "usage")


def subscription_types() -> tuple[str, ...]:
    """Proxy account types billed as a subscription (plan today)."""
    return _types_with(lambda s: s.billing == "subscription")


def routable_types() -> tuple[str, ...]:
    """account types that can serve proxied traffic (api + plan today)."""
    return _types_with(lambda s: s.routable)


def import_types() -> tuple[str, ...]:
    """Legacy import-driven account types; agent software is separate now."""
    return _types_with(lambda s: s.usage_source == "import")


def sql_in(types: tuple[str, ...]) -> str:
    """'?', '?,?'… placeholders for an ``IN (...?)`` clause.

    Values are always passed as query parameters — never string-interpolated.
    """
    return ",".join("?" for _ in types)


def as_payload() -> dict[str, dict]:
    """Serialize the whole table for GET /api/proxy/account-types (frontend)."""
    return {t: s.to_dict() for t, s in ACCOUNT_TYPES.items()}
