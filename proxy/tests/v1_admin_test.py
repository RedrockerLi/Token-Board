#!/usr/bin/env python3
"""Normalized V2 admin CRUD and independent agent management smoke test."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def latest_version(schema_root: Path, database: str) -> int:
    versions = []
    for path in (schema_root / database / "v2").glob("*.sql"):
        major, minor = path.name.split("_", 1)[0].split("-")
        versions.append(int(major) * 10000 + int(minor))
    return max(versions)


def main() -> None:
    schema_root = Path(sys.argv[1]).resolve()
    project = Path(sys.argv[2]).resolve()
    package_paths = {
        "app": project / "app",
        "app.db": project / "app/db",
        "app.domain": project / "app/domain",
        "app.services": project / "app/services",
    }
    for package, package_path in package_paths.items():
        module = types.ModuleType(package)
        module.__path__ = [str(package_path)]
        sys.modules[package] = module
    load("app.domain.account_types", project / "app/domain/account_types.py")
    load("app.services.fx", project / "app/services/fx.py")
    migrations = load("app.db.migrations", project / "app/db/migrations.py")
    proxy_module = load("app.db.proxy_db", project / "app/db/proxy_db.py")
    billing_module = load("app.db.proxy.billing", project / "app/db/proxy/billing.py")
    from app.db.schema_upgrade import ensure_local_databases

    directory = Path(tempfile.mkdtemp())
    db_path = directory / "token-board.db"
    dashboard_path = directory / "dashboard.db"
    ensure_local_databases(str(db_path), str(dashboard_path), str(schema_root))
    database = proxy_module.ProxyDatabase.__new__(proxy_module.ProxyDatabase)
    database.db_path = str(db_path)
    database.schema_dir = str(schema_root)
    pricing_id = database.create_pricing({
        "model_pattern": "gpt-*", "input_price": 2.0,
        "cache_read_price": 1.0, "output_price": 8.0, "currency": "CNY",
        "slots": [{"start_minute": 0, "end_minute": 1440, "multiplier": 1.0}],
    })
    account_id = database.create_account({
        "name": "metered", "account_type": "api", "base_url": "http://example.test",
        "api_format": "openai", "auth_header": "bearer", "max_concurrency": 8,
        "upstream_keys": ["sk-admin-test"],
    })
    local_key = database.create_key({"account_id": account_id, "label": "admin"})
    aggregate_id = database.create_aggregate({
        "name": "all-models", "entries": [{"pattern": "*", "account_id": account_id,
                                              "upstream_model": "gpt-test"}],
    })
    subscription_id = database.create_agent_subscription({
        "name": "codex-subscription", "valid_from": "2026-08-01",
        "currency": "CNY", "monthly_price": 20,
    })
    software_id = database.create_agent_software({
        "name": "codex", "agent_kind": "codex",
        "subscription_ids": [subscription_id],
    })
    # This smoke test covers the subscription's full first billing period;
    # real bindings otherwise begin at the moment the software is attached.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE agent_subscription_bindings SET valid_from=? "
            "WHERE subscription_id=? AND software_id=?",
            ("2026-08-01T00:00:00Z", subscription_id, software_id),
        )
    inserted = database.insert_agent_usage(
        software_id, "gpt-test", 1_000_000, 100_000, 0, 1_100_000,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "v1-admin-import")
    assert database.get_agent_software()[0]["id"] == software_id
    assert database.get_agent_subscriptions()[0]["id"] == subscription_id
    assert len(database.get_timeout_config()) == 3
    assert database.update_timeout_config("openai", {
        "streaming_first_byte_timeout": 15,
        "streaming_idle_timeout": 30,
        "non_streaming_timeout": 120,
    })
    assert database.update_plan_billing_config({
        "cancellation_mode": "end_of_period",
    })
    fx = sys.modules["app.services.fx"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO fx_rates(base_currency,quote_currency,date,rate) "
            "VALUES('USD','CNY','2026-08-09',7.2)")
        assert fx.get_rate(conn, date="2026-08-09") == 7.2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version(
            schema_root, "token-board")
        assert conn.execute("SELECT count(*) FROM upstream_secrets").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM client_keys WHERE key_value=?",
                            (local_key,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM route_rules WHERE route_set_id=?",
                            (aggregate_id,)).fetchone()[0] == 1
        cost = conn.execute(
            "SELECT equivalent_cost,billed_usage_cost FROM request_log WHERE event_id=?",
            ("v1-admin-import",),).fetchone()
        assert inserted and cost and abs(cost[0] - 2.8) < 1e-9 and cost[1] == 0
    finally:
        conn.close()
    assert database.get_accounts()
    assert database.get_pricing()[0]["id"] == pricing_id
    first_materialize = billing_module.materialize_period_charges(str(db_path))
    second_materialize = billing_module.materialize_period_charges(str(db_path))
    first_agent_materialize = billing_module.materialize_agent_subscription_charges(str(db_path))
    assert first_materialize >= 0
    assert first_agent_materialize >= 1
    assert second_materialize >= 0
    with sqlite3.connect(db_path) as conn:
        charge = conn.execute(
            "SELECT subscription_id,period_start,count(*) "
            "FROM agent_subscription_period_charges "
            "GROUP BY subscription_id,period_start"
        ).fetchone()
        assert charge and charge[0] == subscription_id and charge[2] == 1
    migrations.schema_dir_for = lambda _path, _name: str(schema_root)
    exported = database.export_to_dashboard(
        str(dashboard_path), 0, database.get_max_log_id())
    assert exported["record_count"] == 1
    dashboard = sqlite3.connect(dashboard_path)
    try:
        archived = dashboard.execute(
            "SELECT equivalent_cost,billed_usage_cost FROM daily_usage "
            "WHERE account_id=?", (software_id,)
        ).fetchone()
        assert archived and abs(archived[0] - 2.8) < 1e-9 and archived[1] == 0
        assert dashboard.execute(
            "SELECT recurring_charge FROM monthly_recurring_costs "
            "WHERE account_id=?", (software_id,)
        ).fetchone()[0] == 20
    finally:
        dashboard.close()
    print("V2 admin CRUD passed")


if __name__ == "__main__":
    main()
