"""Upstream account-type semantics — the single source of truth.

`account_type` is a UI/template compatibility value, not a storage-table
identity. Its behavior is defined HERE, as a declarative spec table: what an
account of a given type may do (routing / keys / billing / cooldown / deletion
/ usage source). Code must ask the spec (``spec(t).routable`` …) instead of
comparing the string directly, so adding a type is one row in
:data:`ACCOUNT_TYPES` — not a new branch in every caller.

The C++ proxy consumes the prepared SQLite routing snapshot; this module is
the only account-type behavior specification.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountSubtypeSpec:
    """Behavioral contract of one upstream sub-category under a type."""

    label: str  # 账户弹窗左栏子卡片文案（"<名称> — <说明>"，与类型同约定）
    presets: dict  # 新建表单预填：base_url / monthly_price / currency / api_format
    session_header: bool  # 转发时注入 x-opencode-session 会话头（每对话稳定）
    usage_path: str | None  # 用量查询相对路径（GET + Bearer）；None = 无用量查询
    models_path: str | None = None  # 模型目录相对路径（GET）；None = 默认 "{base}/models"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "presets": dict(self.presets),
            "session_header": self.session_header,
            "usage_path": self.usage_path,
            "models_path": self.models_path,
        }


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
    subtypes: dict[str, AccountSubtypeSpec] = field(default_factory=dict)

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
            "subtypes": {name: sub.to_dict() for name, sub in self.subtypes.items()},
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
        subtypes={
            "opencode-go": AccountSubtypeSpec(
                label="OpenCode Go — $10/月 · 含用量查询",
                presets={
                    "base_url": "https://opencode.ai/zen/go",
                    "monthly_price": 10,
                    "currency": "USD",
                    "api_format": "anthropic",
                },
                session_header=True,
                usage_path="/v1/usage",
                models_path="/v1/models",
            ),
        },
    ),
}


def subtype_spec(subtype: str | None) -> AccountSubtypeSpec | None:
    """Spec for a subtype slug across all types (None when not declared)."""
    if not subtype:
        return None
    for type_spec in ACCOUNT_TYPES.values():
        sub = type_spec.subtypes.get(subtype)
        if sub is not None:
            return sub
    return None


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


def as_payload() -> dict[str, dict]:
    """Serialize the whole table for GET /api/proxy/account-types (frontend)."""
    return {t: s.to_dict() for t, s in ACCOUNT_TYPES.items()}
