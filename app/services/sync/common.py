"""WebDAV-based database sync for multi-machine proxy usage.

Syncs normalized V1 configuration and the dashboard archive.  Historical V0
table names are handled only by ``app.db.schema_upgrade`` before a sync merge.

request_log is NOT synced — it is local-only. Dashboard sync is a
pull-export-upload transaction: **cloud is always the latest; every machine's
local dashboard.db is always a historical version of the cloud**. Progress is
tracked by a single high-water mark (sync_state.last_exported_log_id) that is
advanced only AFTER a successful upload — any failed step rolls back by
discarding the shadow db (no partial state, no per-row markers).
"""

import hashlib
import os
import re
import shutil
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

# These are the persistent V1 tables used by local snapshots and config
# merging.  The upload copy is sanitized separately: usable upstream secrets
# and the WebDAV password stay on the machine, while client_keys (the keys
# accepted by this local proxy) are ordinary synchronized configuration.
V1_CONFIG_TABLES = [
    "accounts", "upstreams", "route_sets", "route_rules", "client_keys",
    "upstream_credentials", "upstream_secrets", "account_importers",
    "billing_contracts",
    "billing_rate_events", "pricing_rules", "pricing_rates", "pricing_slots",
    "pricing_length_tiers",
    "proxy_timeout_config", "upstream_model_catalog", "sync_settings",
    "agent_subscriptions", "agent_subscription_instances",
    "agent_subscription_rate_events", "agent_subscription_bindings",
    "agent_software",
]
CONFIG_TABLE_ALLOWLIST = frozenset(V1_CONFIG_TABLES)
_RUNTIME_TABLES = [
    "request_log", "request_attempts", "billing_period_charges",
    "agent_subscription_period_charges", "agent_subscription_charge_allocations",
    "agent_software_runtime", "fx_rates",
    "sync_state", "sync_config", "perf_events", "in_flight_requests", "session_key_log",
]
RUNTIME_TABLE_DENYLIST = frozenset(_RUNTIME_TABLES)


def is_config_sync_table(name: str) -> bool:
    """Return whether a table is explicitly allowed in a config artifact."""

    return str(name) in CONFIG_TABLE_ALLOWLIST


__all__ = ["V1_CONFIG_TABLES", "CONFIG_TABLE_ALLOWLIST",
           "RUNTIME_TABLE_DENYLIST", "is_config_sync_table"]
