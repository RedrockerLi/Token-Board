"""Validation helpers shared by Agent subscription and software APIs."""

import json

from app.core.time import utc_now
from app.db.proxy.common import _parse_iso_date


_LEGACY_AGENT_DATE_FIELDS = {
    "start_time": "valid_from",
    "effective_at": "effective_on",
    "ends_at": "ends_on",
    "finalized_at": "finalized_on",
    "frozen_at": "frozen_on",
    "created_at": None,
    "updated_at": None,
}


def _iso_start(value: object | None) -> str:
    if value in (None, ""):
        return utc_now().date().isoformat()
    parsed = _parse_iso_date(value)
    if parsed is None:
        raise ValueError("订阅起始日必须是 YYYY-MM-DD")
    return parsed.isoformat()


def _json_object(value: object | None) -> str:
    if value in (None, ""):
        return "{}"
    if not isinstance(value, dict):
        raise ValueError("解析器配置必须是 JSON 对象")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reject_legacy_agent_fields(data: dict) -> None:
    """Reject V1 timestamp fields instead of silently dropping their value."""
    for field, replacement in _LEGACY_AGENT_DATE_FIELDS.items():
        if field in data:
            suffix = f"，请使用 {replacement}" if replacement else ""
            raise ValueError(f"Agent V2 不支持字段 {field}{suffix}")


def _number(value: object, message: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if result < 0:
        raise ValueError("月费不能为负数")
    return result
