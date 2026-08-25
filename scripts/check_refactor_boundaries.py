#!/usr/bin/env python3
"""Static deletion gates for the completed runtime-boundary migration."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _python_files(root: Path):
    yield from (root / "app").rglob("*.py")


def _source_files(root: Path):
    yield from (root / "proxy" / "src").rglob("*.h")
    yield from (root / "proxy" / "src").rglob("*.cpp")


def main(root: Path) -> int:
    failures: list[str] = []

    for path in _python_files(root):
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+[^\n]+\s+import\s+\*", text):
            failures.append(f"wildcard import: {path}")
        if "globals()[" in text or "__dict__.update" in text:
            failures.append(f"runtime namespace injection: {path}")

    for path in (root / "app" / "services" / "sync").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"_webdav_|_upload_versioned_artifact", text):
            failures.append(f"private WebDAV workflow entry: {path}")

    workflow_files = (
        root / "app" / "services" / "sync" / "config_merge.py",
        root / "app" / "services" / "sync" / "config_sync.py",
        root / "app" / "services" / "sync" / "dashboard_sync.py",
        root / "app" / "services" / "sync" / "snapshot.py",
        root / "app" / "services" / "sync" / "webdav.py",
        root / "app" / "db" / "dashboard" / "reconcile.py",
    )
    for path in workflow_files:
        text = path.read_text(encoding="utf-8")
        if "sqlite3.connect" in text:
            failures.append(f"workflow opens SQLite outside profile: {path}")

    source_text = "\n".join(path.read_text(encoding="utf-8")
                             for path in _source_files(root))
    if "UsageTracker" in source_text or "usage_tracker" in source_text:
        failures.append("old C++ UsageTracker source symbol remains")
    if "UsageInfo" in source_text:
        failures.append("old C++ UsageInfo compatibility type remains")
    if re.search(r"UpstreamClient::TransportMetrics\s+UpstreamClient::transport_metrics\s*\(",
                 source_text) is None:
        failures.append("transport_metrics implementation is missing")
    if len(re.findall(
            r"UpstreamClient::TransportMetrics\s+UpstreamClient::transport_metrics\s*\(",
            source_text)) != 1:
        failures.append("transport_metrics has more than one implementation")

    db_cpp = (root / "proxy" / "src" / "store" / "db.cpp").read_text(
        encoding="utf-8")
    for helper in ("kSpoolHeaderBytes", "spool_checksum", "bounded_string",
                   "ReadTransactionGuard"):
        if helper in db_cpp:
            failures.append(f"duplicate spool helper remains in db.cpp: {helper}")

    production_cmake = (root / "proxy" / "CMakeLists.txt").read_text(
        encoding="utf-8")
    if production_cmake.count("src/net/upstream_metrics.cpp") != 1:
        failures.append("production CMake does not register one metrics TU")

    if failures:
        print("refactor boundary check failed:")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print("refactor boundary check passed")
    return 0


if __name__ == "__main__":
    repository = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    raise SystemExit(main(repository))
