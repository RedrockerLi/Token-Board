"""Machine-readable HTTP status/business-state contract.

This is intentionally data, not a global exception handler.  Route functions
still choose their existing response body and only use this table to make the
status mapping reviewable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class RouteContract:
    name: str
    path: str
    methods: tuple[str, ...]
    statuses: Mapping[str, int]
    response_shapes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    messages: Mapping[str, str] = field(default_factory=dict)
    json_mode: str = "none"

    @property
    def force_json(self) -> bool:
        """Compatibility-readable view of the request JSON contract."""

        return self.json_mode == "force"

    @property
    def silent_json(self) -> bool:
        return self.json_mode == "silent"


ROUTE_CONTRACTS = {
    "dashboard_delete": RouteContract(
        "dashboard_delete", "/api/proxy/dashboard/users", ("DELETE",),
        {"ok": 200, "not_found": 404, "error": 400},
        response_shapes={
            "ok": ("status", "message"),
            "not_found": ("status", "message"),
            "error": ("status", "message"),
        },
        messages={"error": "用户名称不能为空"}, json_mode="silent"),
    "dashboard_upload": RouteContract(
        "dashboard_upload", "/api/proxy/dashboard/users/upload", ("POST",),
        {"ok": 200, "conflict": 409, "error": 502},
        response_shapes={
            "ok": ("status", "message"),
            "conflict": ("status", "message"),
            "error": ("status", "message"),
        }),
    "config_test": RouteContract(
        "config_test", "/api/proxy/sync/test", ("POST",),
        {"ok": 200, "error": 400},
        response_shapes={
            "ok": ("status", "message"),
            "error": ("error",),
        }, json_mode="force"),
    "config_upload": RouteContract(
        "config_upload", "/api/proxy/sync/config/upload", ("POST",),
        {"ok": 200, "unconfigured": 200, "remote_updated": 200,
         "conflict": 200, "error": 200},
        response_shapes={
            "ok": ("status", "message"),
            "unconfigured": ("status", "message", "conflict"),
            "remote_updated": ("status", "message", "conflict"),
            "conflict": ("status", "message", "conflict"),
            "error": ("status", "message", "conflict"),
        }),
    "key_delete": RouteContract(
        "key_delete", "/api/proxy/keys/<int:key_id>", ("DELETE",),
        {"ok": 200, "not_found": 404, "error": 409},
        response_shapes={
            "ok": ("status",), "not_found": ("error",), "error": ("error",),
        }),
    "resource_create": RouteContract(
        "resource_create", "/api/proxy/accounts", ("POST",),
        {"ok": 201, "error": 400},
        response_shapes={"ok": ("id",), "error": ("error",)},
        json_mode="force"),
}


def status_for(contract_name: str, payload: Mapping[str, object],
               default: int = 200) -> int:
    contract = ROUTE_CONTRACTS[contract_name]
    return int(contract.statuses.get(str(payload.get("status")), default))


__all__ = ["ROUTE_CONTRACTS", "RouteContract", "status_for"]
