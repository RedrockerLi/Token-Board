"""Shared identity, locking and shadow helpers for the V0→V1 transition."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import sqlite3
import struct
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NAMESPACE = uuid.UUID("646d9175-a4f5-4caa-aaf4-98362b8fd550")
LEGACY_CREDENTIAL_UUID = "00000000-0000-5000-8000-000000000001"

_spec = importlib.util.spec_from_file_location(
    "token_board_migrations", REPO / "app" / "db" / "migrations.py")
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
migrate = _module.migrate


def stable_uuid(kind: str, *parts: object) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join([kind, *(str(p) for p in parts)])))


def mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:3] + "…" if len(key) <= 10 else f"{key[:6]}…{key[-4:]}"


def utc_timestamp(value: str | None, source_tz: ZoneInfo) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10:
        return text
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_tz)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def migration_locks(proxy: Path, dashboard: Path):
    handles = []
    try:
        for path in (proxy, dashboard):
            lock = open(str(path) + ".migrate.lock", "a+b")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            handles.append(lock)
        yield
    finally:
        for lock in reversed(handles):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def assert_offline_and_checkpoint(path: Path) -> None:
    conn = sqlite3.connect(path, timeout=1, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise RuntimeError(f"WAL checkpoint busy for {path}: {result}")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(f"database is still in use: {path}: {exc}") from exc
    finally:
        conn.close()


def assert_spool_empty(proxy: Path) -> None:
    spool = Path(str(proxy) + ".request-log.spool")
    if spool.exists() and spool.stat().st_size:
        raise RuntimeError(
            f"request accounting spool is not empty ({spool.stat().st_size} bytes): {spool}")


def read_usage_spool(proxy: Path) -> list[dict]:
    """Decode durable C++ request-log frames for an offline transition.

    Frames use ``uint32_le length + uint32_le FNV-1a checksum + JSON``.  A
    partial or corrupt frame is an error, never silently discarded; the source
    spool remains untouched and is restored from the manifest on rollback.
    """
    spool = Path(str(proxy) + ".request-log.spool")
    if not spool.is_file() or spool.stat().st_size == 0:
        return []
    payload = spool.read_bytes()
    records, offset = [], 0
    while offset < len(payload):
        if len(payload) - offset < 8:
            raise RuntimeError(f"incomplete request spool header at {offset}")
        size, expected = struct.unpack_from("<II", payload, offset)
        offset += 8
        if size <= 0 or size > 256 * 1024 or offset + size > len(payload):
            raise RuntimeError(f"invalid request spool frame at {offset - 8}")
        frame = payload[offset:offset + size]
        offset += size
        actual = 2166136261
        for byte in frame:
            actual = ((actual ^ byte) * 16777619) & 0xffffffff
        if actual != expected:
            raise RuntimeError(f"request spool checksum mismatch at {offset - size}")
        try:
            record = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid request spool JSON at {offset - size}") from exc
        if not isinstance(record, dict) or record.get("v") != 1 or not record.get("event_id"):
            raise RuntimeError(f"unsupported request spool record at {offset - size}")
        records.append(record)
    return records


def source_version(path: Path, expected_minor: int) -> None:
    conn = sqlite3.connect(path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        major, minor = divmod(version, 10_000)
        if major != 0 or minor != expected_minor:
            raise RuntimeError(
                f"{path} must be V0.{expected_minor}, found V{major}.{minor}")
    finally:
        conn.close()


def shadow_path(source: Path) -> Path:
    return source.with_name(source.name + ".v1-shadow")


def prepare_shadow(source: Path, database_name: str, schema_root: Path) -> Path:
    shadow = shadow_path(source)
    if shadow.exists():
        raise RuntimeError(f"shadow already exists; resume or move it aside: {shadow}")
    migrate(str(shadow), str(schema_root), database_name)
    return shadow
