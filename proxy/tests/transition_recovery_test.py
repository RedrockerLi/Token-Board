#!/usr/bin/env python3
"""V0→V1 apply/rollback preserves every managed transition artifact."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def latest_version(schema_root: Path, database: str) -> int:
    versions = []
    for path in (schema_root / database / "v1").glob("*.sql"):
        major, minor = path.name.split("_", 1)[0].split("-")
        versions.append(int(major) * 10000 + int(minor))
    return max(versions)


def main() -> None:
    schema = Path(sys.argv[1]).resolve()
    project = Path(sys.argv[2]).resolve()
    spec = importlib.util.spec_from_file_location(
        "transition_test_migrations", project / "app/db/migrations.py")
    assert spec is not None and spec.loader is not None
    migrations = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = migrations
    spec.loader.exec_module(migrations)
    migrate = migrations.migrate

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proxy = root / "token-board.db"
        dashboard = root / "dashboard.db"
        migrate(str(proxy), str(schema / "token-board/v0"), "token-board")
        migrate(str(dashboard), str(schema / "dashboard/v0"), "dashboard")
        with sqlite3.connect(proxy) as conn:
            conn.execute(
                "INSERT INTO upstream_accounts(name,base_url,account_type,created_at) "
                "VALUES('transition','http://example.test','api','2026-01-01 08:00:00')"
            )
        snapshot = root / "config_snapshot.db"
        snapshot.write_bytes(b"original-v0-snapshot")
        sync = root / "sync_config.json"
        sync.write_text('{"remote":"v0"}', encoding="utf-8")
        originals = {path: digest(path) for path in (snapshot, sync)}

        command = [
            sys.executable,
            str(schema / "transitions/0-to-1/migrate.py"),
            "--token-board-db", str(proxy),
            "--dashboard-db", str(dashboard),
            "--schema-dir", str(schema),
            "--timezone", "Asia/Shanghai",
            "--confirm-timezone", "Asia/Shanghai",
            "--apply",
        ]
        applied = subprocess.run(command, capture_output=True, text=True)
        assert applied.returncode == 0, applied.stdout + applied.stderr
        manifests = list(root.glob("v0-to-v1-*.manifest.json"))
        assert len(manifests) == 1
        manifest_path = manifests[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["stage"] == "complete"
        assert manifest["timezone_samples"][0] == {
            "source": "2026-01-01 08:00:00",
            "utc": "2026-01-01T00:00:00Z",
        }
        backed = {Path(item["source"]).name for item in manifest["backups"]
                  if item.get("existed", True)}
        assert {"token-board.db", "dashboard.db", "config_snapshot.db",
                "sync_config.json"} <= backed
        assert version(proxy) == latest_version(schema, "token-board")
        assert version(dashboard) == latest_version(schema, "dashboard")
        assert version(snapshot) == latest_version(schema, "token-board")

        subprocess.run(
            [sys.executable, str(schema / "transitions/0-to-1/migrate.py"),
             "--rollback-manifest", str(manifest_path)],
            check=True, capture_output=True, text=True,
        )
        assert version(proxy) == 19 and version(dashboard) == 6
        for item in manifest["backups"]:
            source = Path(item["source"])
            if source in {proxy, dashboard}:
                assert digest(source) == item["sha256"]
        for path, expected in originals.items():
            assert digest(path) == expected, f"rollback changed {path.name}"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["stage"] == "rolled_back"

        # A stop before shadow creation must be resumable from the durable
        # manifest, not require a manually restarted migration.
        interrupted = subprocess.run(
            command + ["--inject-failure", "backed_up"],
            capture_output=True, text=True,
        )
        assert interrupted.returncode != 0
        pending = [path for path in root.glob("v0-to-v1-*.manifest.json")
                   if json.loads(path.read_text(encoding="utf-8"))["stage"]
                   == "backed_up"]
        assert len(pending) == 1
        subprocess.run(
            [sys.executable, str(schema / "transitions/0-to-1/migrate.py"),
             "--resume-manifest", str(pending[0])],
            check=True, capture_output=True, text=True,
        )
        assert version(proxy) == latest_version(schema, "token-board")
        assert version(dashboard) == latest_version(schema, "dashboard")
    print("transition apply/rollback recovery passed")


if __name__ == "__main__":
    main()
