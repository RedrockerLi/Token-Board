#!/usr/bin/env python3
"""V2 config merge preserves machine-local secrets while syncing config."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import types
from pathlib import Path


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed(path: Path, name: str, runtime_id: int, secret: str | None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO accounts(id,uuid,name,account_kind) "
                 "VALUES(1,'account-uuid',?,'proxy')", (name,))
    conn.execute("INSERT INTO upstreams(id,account_id,name,base_url) VALUES(1,1,?,'http://upstream')",
                 (name,))
    conn.execute("INSERT INTO route_sets(id,uuid,account_id,name) VALUES(1,'route-uuid',1,?)",
                 (name,))
    conn.execute("INSERT INTO route_rules(id,route_set_id,upstream_id) VALUES(1,1,1)")
    conn.execute(
        "INSERT INTO upstream_credentials(uuid,runtime_id,upstream_id,key_masked,enabled) "
        "VALUES('credential-uuid',?,1,'sk-…test',1)", (runtime_id,))
    if secret:
        conn.execute("INSERT INTO upstream_secrets VALUES('credential-uuid',?,"
                     "strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (secret,))
    conn.commit()
    conn.close()


def main() -> None:
    schema_root = Path(sys.argv[1]).resolve()
    project = Path(sys.argv[2]).resolve()
    for package, package_path in {
        "app": project / "app",
        "app.db": project / "app/db",
        "app.services": project / "app/services",
    }.items():
        module = types.ModuleType(package)
        module.__path__ = [str(package_path)]
        sys.modules[package] = module
    migrations = load("app.db.migrations", project / "app/db/migrations.py")
    from app.db.schema_upgrade import ensure_local_databases
    from app.services.sync.config_merge import merge_config_tables
    directory = Path(tempfile.mkdtemp())
    local, remote = directory / "local.db", directory / "remote.db"
    local_dashboard = directory / "local.dashboard.db"
    remote_dashboard = directory / "remote.dashboard.db"
    ensure_local_databases(str(local), str(local_dashboard), str(schema_root))
    ensure_local_databases(str(remote), str(remote_dashboard), str(schema_root))
    seed(local, "local", 77, "sk-local-secret")
    seed(remote, "remote", 1, None)
    merge_config_tables(str(remote), str(local))
    conn = sqlite3.connect(local)
    try:
        assert conn.execute("SELECT name FROM accounts WHERE id=1").fetchone()[0] == "remote"
        assert conn.execute("SELECT runtime_id FROM upstream_credentials").fetchone()[0] == 77
        assert conn.execute("SELECT secret_value FROM upstream_secrets").fetchone()[0] == "sk-local-secret"
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        conn.close()
    print("V2 config sync passed")


if __name__ == "__main__":
    main()
