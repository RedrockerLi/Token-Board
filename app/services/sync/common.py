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

# V1 configuration is metadata only.  Credential secrets, request history,
# import cursors, and sync watermarks remain machine-local.  Keeping the lists
# here makes upload sanitization and hash calculation use the same contract.
V1_CONFIG_TABLES = [
    "accounts", "upstreams", "route_sets", "route_rules", "client_keys",
    "upstream_credentials", "account_importers", "billing_contracts",
    "billing_rate_events", "pricing_rules", "pricing_rates", "pricing_slots",
    "proxy_timeout_config", "upstream_model_catalog", "sync_settings",
]
_RUNTIME_TABLES = [
    "request_log", "request_attempts", "upstream_secrets", "billing_period_charges",
    "fx_rates", "sync_state", "sync_config", "perf_events", "in_flight_requests",
    "session_key_log",
]


__all__ = [name for name in globals() if not name.startswith('__')]
